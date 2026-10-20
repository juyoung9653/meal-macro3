import unittest

import pandas as pd

from src.config import COL_DONG, COL_MEAL, COL_NAME, COL_ORD, COL_PHONE
from src.excel.groups import build_sheet_layout, get_groups
from src.excel.sheet_writer import box_group_ranges


class SheetLayoutTests(unittest.TestCase):
    def test_places_per_dong_meal_size_table_after_data(self) -> None:
        df = pd.DataFrame(
            {
                COL_ORD: [1, 1, 2, 3, 3],
                COL_DONG: ["가동", "가동", "가동", "나동", "나동"],
                COL_NAME: ["가", "나", "다", "라", "마"],
                COL_PHONE: ["010-1", "0101", "010-2", "010-3", "0103"],
            }
        )

        layout = build_sheet_layout(df)

        self.assertEqual(
            [
                (g.source_start, g.source_end, g.start_row, g.end_row)
                for g in layout.groups
            ],
            [(0, 1, 3, 4), (2, 2, 5, 5), (3, 4, 6, 7)],
        )
        self.assertEqual(layout.meal_sizes, (1, 2, 3, 4, 5))
        self.assertEqual(
            [
                (s.label, s.household_counts, s.total_meals, s.row)
                for s in layout.dong_meal_summaries
            ],
            [
                ("가동", (1, 1, 0, 0, 0), 3, 9),
                ("나동", (0, 1, 0, 0, 0), 2, 10),
            ],
        )
        self.assertIsNotNone(layout.overall_summary)
        self.assertEqual(
            (
                layout.overall_summary.label,
                layout.overall_summary.household_counts,
                layout.overall_summary.total_meals,
                layout.overall_summary.row,
            ),
            ("전체", (1, 2, 0, 0, 0), 5, 11),
        )
        self.assertEqual(layout.data_end_row, 7)
        self.assertEqual(layout.summary_header_row, 8)
        self.assertEqual(layout.used_rows, 11)
        self.assertEqual(layout.used_cols, 7)

    def test_different_phones_stay_separate_even_with_same_order(self) -> None:
        df = pd.DataFrame(
            {
                COL_ORD: [1, 1],
                COL_DONG: ["가동", "가동"],
                COL_NAME: ["가", "나"],
                COL_PHONE: ["010-1111-1111", "010-2222-2222"],
            }
        )

        self.assertEqual(get_groups(df), [(0, 0), (1, 1)])

    def test_phone_formatting_is_ignored_and_blank_phones_stay_separate(self) -> None:
        df = pd.DataFrame(
            {
                COL_PHONE: [
                    "010-1234-5678",
                    "010 1234 5678",
                    "",
                    None,
                ]
            }
        )

        self.assertEqual(get_groups(df), [(0, 1), (2, 2), (3, 3)])

    def test_repeated_dong_sections_are_combined_in_one_summary_row(self) -> None:
        df = pd.DataFrame(
            {
                COL_ORD: [1, 2, 3],
                COL_DONG: ["가동", "나동", "가동"],
                COL_NAME: ["가", "나", "다"],
                COL_PHONE: ["010-1", "010-2", "010-3"],
            }
        )

        layout = build_sheet_layout(df)

        self.assertEqual(
            [(s.label, s.household_counts) for s in layout.dong_meal_summaries],
            [("가동", (2, 0, 0, 0, 0)), ("나동", (1, 0, 0, 0, 0))],
        )

    def test_without_dong_does_not_create_dong_summary(self) -> None:
        df = pd.DataFrame(
            {
                COL_ORD: [1, 2],
                COL_NAME: ["가", "나"],
                COL_PHONE: ["010-1", "010-2"],
            }
        )

        layout = build_sheet_layout(df)

        self.assertEqual(layout.dong_meal_summaries, ())
        self.assertIsNone(layout.summary_header_row)
        self.assertIsNone(layout.overall_summary)
        self.assertEqual(layout.data_end_row, len(df) + 2)
        self.assertEqual(layout.used_rows, layout.data_end_row)

    def test_hidden_grouping_columns_still_create_summary(self) -> None:
        summary_df = pd.DataFrame(
            {
                COL_ORD: [1, 1, 2],
                COL_DONG: ["가동", "가동", "나동"],
                COL_NAME: ["가", "나", "다"],
                "상세주소": ["101호", "101호", "202호"],
                COL_PHONE: ["010-1", "0101", "010-2"],
            }
        )
        visible_df = summary_df[[COL_NAME, "상세주소"]]

        layout = build_sheet_layout(
            summary_df,
            display_col_count=len(visible_df.columns) + 1,
        )

        self.assertEqual(
            [(s.label, s.household_counts) for s in layout.dong_meal_summaries],
            [("가동", (0, 1, 0, 0, 0)), ("나동", (1, 0, 0, 0, 0))],
        )
        self.assertIsNotNone(layout.summary_header_row)
        self.assertEqual(layout.used_cols, 7)

    def test_box_group_ranges_include_the_whole_household(self) -> None:
        source_df = pd.DataFrame(
            {
                COL_ORD: [1, 1, 2],
                COL_DONG: ["same", "same", "same"],
                COL_MEAL: ["base", "base_BOX", "base"],
                COL_PHONE: ["010-1", "0101", "010-2"],
            }
        )
        layout = build_sheet_layout(source_df)

        self.assertEqual(box_group_ranges(source_df, layout), [(3, 4)])

    def test_box_group_ranges_are_empty_without_meal_column(self) -> None:
        source_df = pd.DataFrame(
            {COL_ORD: [1], COL_DONG: ["same"], COL_PHONE: ["010-1"]}
        )
        layout = build_sheet_layout(source_df)

        self.assertEqual(box_group_ranges(source_df, layout), [])


if __name__ == "__main__":
    unittest.main()
