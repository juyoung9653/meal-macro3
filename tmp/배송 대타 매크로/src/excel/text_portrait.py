import pandas as pd

from src.config import COL_ADDR, COL_NOTE


def _wrap_tokens(tokens: list[str], limit: int = 10) -> str:
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0

    for t in (x for x in tokens if x):
        add = len(t) if not cur else len(t) + 1
        if cur and cur_len + add > limit:
            out.append(" ".join(cur))
            cur = [t]
            cur_len = len(t)
        else:
            cur.append(t)
            cur_len += add

    if cur:
        out.append(" ".join(cur))
    return "\n".join(out)


def preprocess_portrait(df: pd.DataFrame) -> pd.DataFrame:
    addr_col = COL_ADDR
    note_col = COL_NOTE
    if df.empty or (addr_col not in df.columns and note_col not in df.columns):
        return df

    d = df.copy()

    if addr_col in d.columns:
        # 1. 엑셀의 모든 줄바꿈(\r, \n)을 공백으로 치환하고 앞뒤 공백 제거
        s = d[addr_col].fillna("").astype(str)
        s = s.str.replace(r"[\r\n]+", " ", regex=True).str.strip()

        # 2. 도로명 주소 패턴 (길/로 + 숫자) 뒤에 줄바꿈 삽입
        # 패턴: 길/로 + 공백(있을수도없을수도) + 숫자(-숫자)
        # 그 뒤에 오는 공백이나 쉼표를 \n으로 교체
        pattern = r"(([로|길])\s*\d+(?:-\d+)?)(?:[,\s]+)"
        s = s.str.replace(pattern, r"\1\n", regex=True)
        d[addr_col] = s.str.replace("  ", " ")

    if note_col in d.columns:
        # 비고란도 동일하게 기존 줄바꿈을 먼저 제거
        s = d[note_col].fillna("").astype(str)
        s = s.str.replace(r"[\r\n]+", " ", regex=True).str.strip()
        d[note_col] = s.map(lambda x: _wrap_tokens(x.split(" "), 8))

    return d
