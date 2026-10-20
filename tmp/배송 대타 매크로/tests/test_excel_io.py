import unittest
from types import SimpleNamespace

import pandas as pd

from src.config import COL_DONG, COL_MEAL, COL_NAME, COL_ORD, COL_SHIP
from src.excel_io import Choice, active_combinations, make_result, read_df


class ReadTableTests(unittest.TestCase):
    def sheet(self, headers, body):
        table = SimpleNamespace(
            Name="db",
            HeaderRowRange=SimpleNamespace(Value=headers),
            DataBodyRange=body,
        )
        return SimpleNamespace(Name="종합_jon(DB)", ListObjects=[table])

    def test_reads_only_table_headers_and_body(self):
        # No UsedRange/Range: reading fixed worksheet coordinates must fail.
        ws = self.sheet(((" 이름 ", "20260909"),),
                        SimpleNamespace(Value=(("가", 1), ("나", 0))))
        ws.ListObjects.insert(0, SimpleNamespace(Name="other"))
        result = read_df(ws)
        self.assertEqual(result.columns.tolist(), ["이름", "20260909"])
        self.assertEqual(result.values.tolist(), [["가", 1], ["나", 0]])

    def test_empty_table_preserves_headers(self):
        result = read_df(self.sheet((("이름", "배송순서"),), None))
        self.assertTrue(result.empty)
        self.assertEqual(result.columns.tolist(), ["이름", "배송순서"])

    def test_single_cell_table(self):
        ws = self.sheet("이름", SimpleNamespace(Value="가"))
        ws.ListObjects[0].Name = "DB"
        self.assertEqual(read_df(ws).values.tolist(), [["가"]])

    def test_blank_single_cell_is_still_a_data_row(self):
        result = read_df(self.sheet("이름", SimpleNamespace(Value=None)))
        self.assertEqual(result.shape, (1, 1))
        self.assertIsNone(result.iloc[0, 0])

    def test_missing_table_reports_name(self):
        ws = SimpleNamespace(Name="종합_jon(DB)", ListObjects=[])
        with self.assertRaisesRegex(ValueError, "'db' 테이블"):
            read_df(ws)


class MakeResultMealFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.df = pd.DataFrame(
            {
                COL_MEAL: ["기본식", "기본식_BOX", "기본식_중지", "다른식"],
                COL_SHIP: ["담당A"] * 4,
                "20260812": [1, 1, 1, 1],
                COL_ORD: [3, 1, 2, 4],
                COL_DONG: ["가동"] * 4,
                COL_NAME: ["가", "나", "다", "라"],
            }
        )
        self.cols = [COL_MEAL, COL_ORD, COL_DONG, COL_NAME]

    def test_selected_meal_includes_its_box_variant(self) -> None:
        result = make_result(
            self.df,
            Choice("기본식", "담당A", "20260812", self.cols),
        )

        self.assertEqual(result[COL_MEAL].tolist(), ["기본식_BOX", "기본식"])
        self.assertNotIn("기본식_중지", result[COL_MEAL].tolist())

    def test_selecting_box_variant_does_not_duplicate_suffix(self) -> None:
        result = make_result(
            self.df,
            Choice("기본식_BOX", "담당A", "20260812", self.cols),
        )

        self.assertEqual(result[COL_MEAL].tolist(), ["기본식_BOX"])

    def test_active_combinations_only_returns_selected_date_options(self) -> None:
        self.df.loc[2, "20260812"] = 0
        self.df.loc[3, "20260812"] = 0

        combinations = active_combinations(self.df, "20260812")

        self.assertEqual(combinations, [("기본식", "담당A")])

    def test_active_combinations_returns_empty_for_unknown_date(self) -> None:
        self.assertEqual(active_combinations(self.df, "20260901"), [])


if __name__ == "__main__":
    unittest.main()
