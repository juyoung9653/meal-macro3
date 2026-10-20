import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from src.config import (
    COL_DONG,
    COL_MEAL,
    COL_ORD,
    COL_PHONE,
    COL_SHIP,
    MAX_ROWS,
    PICK_COLS,
    SHEET_NAME,
)
from src.data_store import load_settings, save_settings
from src.excel.viewer import show_excel
from src.excel_io import Choice, active_combinations, load_excel, make_result, txt, uniq

ORI_OPTIONS = ["자동", "세로", "가로"]
SPLIT_OPTIONS = ["사용 안함", "동기준", "행기준"]


def ask_pw(root: tk.Tk) -> str | None:
    return simpledialog.askstring(
        "비밀번호", "db파일 비밀번호를 입력해 주세요.", show="*", parent=root
    )


def run_gui() -> None:
    root = tk.Tk()
    root.title("필터 선택")
    root.withdraw()

    pw = ask_pw(root)
    if pw is None:
        return

    st = load_settings(PICK_COLS)

    root.deiconify()

    db_v = tk.StringVar(value=st.db_path)
    status_v = tk.StringVar(value="DB 파일을 선택해 주세요.")

    # 날짜 기본값: 오늘(YYYYMMDD)
    v_flag = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))

    v_meal = tk.StringVar(value="")
    v_ship = tk.StringVar(value="")
    v_ori = tk.StringVar(value=st.orientation)
    v_split = tk.StringVar(value=st.split_mode)

    # 최대 행 입력 변수 추가 (기본값 20)
    v_max_rows = tk.StringVar(value=str(MAX_ROWS))

    on = {c: tk.BooleanVar(value=st.cols.enabled.get(c, True)) for c in PICK_COLS}
    order = st.cols.order[:]

    df = None
    meals: list[str] = []
    ships: list[str] = []

    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.columnconfigure(1, weight=1)
    frm.columnconfigure(3, weight=1)  # 3열 가중치 추가

    # DB 파일 선택 UI
    ttk.Label(frm, text="DB 파일").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Label(frm, textvariable=db_v).grid(
        row=0, column=1, columnspan=2, sticky="ew", pady=4
    )
    ttk.Button(frm, text="찾기...", command=lambda: on_pick_db()).grid(
        row=0, column=3, sticky="ew", pady=4, padx=(5, 0)
    )

    # 식사 구분
    ttk.Label(frm, text="식사 구분").grid(row=1, column=0, sticky="w", pady=4)
    meal_cb = ttk.Combobox(frm, textvariable=v_meal, values=meals, state="readonly")
    meal_cb.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)

    # 배송담당
    ttk.Label(frm, text="배송담당").grid(row=2, column=0, sticky="w", pady=4)
    ship_cb = ttk.Combobox(frm, textvariable=v_ship, values=ships, state="readonly")
    ship_cb.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)

    # 날짜
    ttk.Label(frm, text="날짜").grid(row=3, column=0, sticky="w", pady=4)
    date_entry = ttk.Entry(frm, textvariable=v_flag)
    date_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=4)

    # --- 인쇄 설정 (방향, 분할, 최대행 한 줄 배치) ---
    option_frm = ttk.Frame(frm)
    option_frm.grid(row=4, column=0, columnspan=4, sticky="ew", pady=10)
    option_frm.columnconfigure(1, weight=1)
    option_frm.columnconfigure(3, weight=1)

    ttk.Label(option_frm, text="인쇄 방향").grid(
        row=0, column=0, sticky="w", padx=(0, 5)
    )
    ttk.Combobox(
        option_frm, textvariable=v_ori, state="readonly", values=ORI_OPTIONS, width=10
    ).grid(row=0, column=1, sticky="ew")

    ttk.Label(option_frm, text=" 분할").grid(row=0, column=2, sticky="w", padx=(10, 5))
    ttk.Combobox(
        option_frm,
        textvariable=v_split,
        state="readonly",
        values=SPLIT_OPTIONS,
        width=10,
    ).grid(row=0, column=3, sticky="ew")

    ttk.Label(option_frm, text=" 최대 그룹").grid(
        row=0, column=4, sticky="w", padx=(10, 5)
    )
    ttk.Entry(option_frm, textvariable=v_max_rows, width=5).grid(
        row=0, column=5, sticky="ew"
    )

    # 컬럼 선택/순서
    box = ttk.LabelFrame(frm, text="출력 컬럼(체크=활성) / 순서(위·아래)", padding=10)
    box.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(6, 6))
    box.columnconfigure(0, weight=1)

    lb = tk.Listbox(box, height=8, exportselection=False)
    lb.grid(row=0, column=0, sticky="nsew")

    btns = ttk.Frame(box)
    btns.grid(row=0, column=1, sticky="ns", padx=(10, 0))

    # 상태 표시 바
    ttk.Label(frm, textvariable=status_v).grid(
        row=6, column=0, columnspan=4, sticky="w", pady=(2, 0)
    )

    def persist() -> None:
        save_settings(
            db_path=txt(db_v.get()),
            order=order[:],
            enabled={c: bool(on[c].get()) for c in PICK_COLS},
        )

    def render() -> None:
        lb.delete(0, tk.END)
        for c in order:
            lb.insert(tk.END, f"[{'✓' if on[c].get() else ' '}] {c}")

    def cur() -> int | None:
        sel = lb.curselection()
        return int(sel[0]) if sel else None

    def toggle(_=None) -> None:
        i = cur()
        if i is None:
            return
        c = order[i]
        on[c].set(not on[c].get())
        render()
        lb.selection_set(i)
        persist()

    def move(d: int) -> None:
        i = cur()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            render()
            lb.selection_set(j)
            persist()

    def refresh_filter_choices(_=None) -> list[tuple[str, str]]:
        """현재 날짜에 실제 배송이 있는 식종과 담당만 선택지로 표시합니다."""
        if df is None:
            return []

        combinations = active_combinations(df, v_flag.get())
        if not combinations:
            meal_cb["values"] = meals
            ship_cb["values"] = ships
            if not txt(v_meal.get()) and meals:
                v_meal.set(meals[0])
            if not txt(v_ship.get()) and ships:
                v_ship.set(ships[0])
            return []

        active_meals = sorted({meal for meal, _ in combinations})
        meal_cb["values"] = active_meals
        if txt(v_meal.get()) not in active_meals:
            v_meal.set(active_meals[0])

        selected_meal = txt(v_meal.get())
        active_ships = sorted(
            {ship for meal, ship in combinations if meal == selected_meal}
        )
        ship_cb["values"] = active_ships
        if txt(v_ship.get()) not in active_ships:
            v_ship.set(active_ships[0])
        return combinations

    def no_result_message(flag_col: str, combinations: list[tuple[str, str]]) -> str:
        selected = (
            f"날짜: {flag_col}\n"
            f"식사 구분: {txt(v_meal.get()) or '(미선택)'}\n"
            f"배송담당: {txt(v_ship.get()) or '(미선택)'}"
        )
        if not combinations:
            return "선택한 날짜에 배송 표시(1)된 대상이 없습니다.\n\n" + selected

        by_meal: dict[str, list[str]] = {}
        for meal, ship in combinations:
            by_meal.setdefault(meal, []).append(ship)
        available = "\n".join(
            f"- {meal}: {', '.join(available_ships)}"
            for meal, available_ships in by_meal.items()
        )
        return (
            "선택한 식종과 배송담당 조합의 결과가 0건입니다.\n\n"
            f"{selected}\n\n"
            f"{flag_col} 날짜에 가능한 조합:\n{available}"
        )

    def load_db() -> bool:
        nonlocal df, meals, ships
        p = Path(txt(db_v.get()))
        if not p.exists():
            status_v.set("DB 파일 경로가 올바르지 않습니다.")
            return False
        try:
            status_v.set("DB 로딩 중...")
            root.update_idletasks()
            df = load_excel(p, SHEET_NAME, pw)
            meals = uniq(df, COL_MEAL)
            ships = uniq(df, COL_SHIP)
            combinations = refresh_filter_choices()
            status_v.set(
                f"DB 로딩 완료 · {txt(v_flag.get())} 가능 조합 {len(combinations)}개"
                if combinations
                else "DB 로딩 완료 · 선택 날짜 배송 없음"
            )
            return True
        except Exception as e:
            df = None
            status_v.set("DB 로딩 실패")
            messagebox.showerror("오류", str(e))
            return False

    def on_pick_db() -> None:
        fn = filedialog.askopenfilename(
            parent=root,
            title="DB 엑셀 파일 선택",
            filetypes=[("Excel files", "*.xlsx;*.xlsm;*.xls"), ("All files", "*.*")],
        )
        if not fn:
            return
        db_v.set(fn)
        persist()
        load_db()

    def on_run() -> None:
        nonlocal df
        if df is None:
            if not load_db():
                return

        f = txt(v_flag.get())
        if not f:
            messagebox.showerror("오류", "날짜(플래그) 컬럼명을 입력해 주세요.")
            return

        combinations = refresh_filter_choices()

        cols = [c for c in order if on[c].get()]
        if not cols:
            messagebox.showerror("오류", "출력할 컬럼을 1개 이상 선택해 주세요.")
            return

        # 최대 행 수 검증
        try:
            max_rows_val = int(v_max_rows.get())
        except ValueError:
            messagebox.showerror("오류", "최대 그룹 수에 숫자만 입력해 주세요.")
            return

        persist()

        # 관할동/배송순서/핸드폰은 출력 체크 여부와 관계없이 가구 묶음과 하단
        # 요약에 필요합니다. 필터 결과에는 내부 계산용 열을 남기고, Excel에는
        # 사용자가 체크한 열만 전달합니다.
        result_cols = list(
            dict.fromkeys([*cols, COL_ORD, COL_DONG, COL_MEAL, COL_PHONE])
        )
        try:
            result = make_result(
                df,
                Choice(txt(v_meal.get()), txt(v_ship.get()), f, result_cols),
            )
        except KeyError as e:
            status_v.set("날짜 헤더 확인 필요")
            messagebox.showerror("날짜 오류", str(e))
            return

        if result.empty:
            status_v.set("결과 0건 · 조건 확인 필요")
            messagebox.showwarning("결과 없음", no_result_message(f, combinations))
            return

        out = result.loc[:, [col for col in cols if col in result.columns]]

        status_v.set(f"결과 {len(out)}건 · Excel 생성 중...")
        root.update_idletasks()
        try:
            show_excel(
                df=out,
                summary_df=result,
                title="결과",
                meal=v_meal.get(),
                ship=v_ship.get(),
                date_str=f,
                ori=ORI_OPTIONS.index(v_ori.get()),
                split=SPLIT_OPTIONS.index(v_split.get()),
                max_rows=max_rows_val,
            )
        except Exception as e:
            status_v.set("Excel 생성 실패")
            messagebox.showerror("Excel 생성 실패", str(e))
            return
        status_v.set(f"완료 · {len(out)}건")
        messagebox.showinfo("완료", "완료되었습니다")

    ttk.Button(btns, text="위", width=6, command=lambda: move(-1)).pack(pady=(0, 6))
    ttk.Button(btns, text="아래", width=6, command=lambda: move(1)).pack()
    ttk.Button(btns, text="토글", width=6, command=toggle).pack(pady=(10, 0))
    lb.bind("<Double-Button-1>", toggle)
    lb.bind("<Return>", toggle)
    meal_cb.bind("<<ComboboxSelected>>", refresh_filter_choices)
    date_entry.bind("<Return>", refresh_filter_choices)
    date_entry.bind("<FocusOut>", refresh_filter_choices)

    render()
    if order:
        lb.selection_set(0)

    ttk.Button(frm, text="실행", command=on_run).grid(
        row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0)
    )

    if txt(db_v.get()):
        load_db()

    root.mainloop()
