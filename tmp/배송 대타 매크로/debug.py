import traceback
from pathlib import Path

import win32com.client as win32
from win32com.client import gencache


def debug_load_excel(file_path: Path, sheet_name: str, password: str = ""):
    target_path = str(file_path.resolve())
    excel = None
    wb = None
    is_new_app = False

    def get_excel_state():
        """현재 엑셀의 가시성 및 상태를 문자열로 반환"""
        try:
            return (
                f"Visible: {excel.Visible}, "
                f"DisplayAlerts: {excel.DisplayAlerts}, "
                f"ScreenUpdating: {excel.ScreenUpdating}"
            )
        except:
            return "상태 확인 불가"

    print(f"--- 디버깅 시작: {target_path} ---")
    print(f"입력된 비밀번호: {'*' * len(password)} (길이: {len(password)})")

    try:
        # 1. 엑셀 인스턴스 확보 (Early Binding 강제)
        try:
            # gencache를 사용하면 해당 PC의 엑셀 라이브러리를 새로 읽어와 인자 전달이 정확해집니다.
            excel = gencache.EnsureDispatch("Excel.Application")
            print(f"[1] 엑셀 어플리케이션 확보 완료. ({get_excel_state()})")
        except Exception as e:
            print(f"[!] EnsureDispatch 실패, 일반 Dispatch 시도: {e}")
            excel = win32.Dispatch("Excel.Application")

        # 2. 초기 강제 숨김 설정
        # 만약 여기서 Visible이 True로 안 바뀐다면 초기 설정은 성공인 것입니다.
        excel.Visible = False
        excel.DisplayAlerts = (
            False  # 중요: False여야 비번이 틀렸을 때 창을 안 띄우고 에러를 냅니다.
        )

        if excel.Visible:
            print("[!] 경고: 엑셀이 Visible=False 설정을 거부하고 있습니다.")
        else:
            print(f"[2] 초기 숨김 및 알람 차단 설정 완료. ({get_excel_state()})")

        # 3. 이미 열려 있는지 확인
        for w in excel.Workbooks:
            if w.FullName.lower() == target_path.lower():
                wb = w
                print(f"[3] 이미 열려 있는 파일을 발견했습니다: {wb.Name}")
                break

        # 4. 제한된 보기(Protected View) 체크
        # 특정 PC에서 창이 뜨는 가장 흔한 이유입니다.
        if not wb:
            for pv in excel.ProtectedViewWindows:
                if pv.SourceFullName.lower() == target_path.lower():
                    print(
                        f"[4] 제한된 보기 창 발견! Edit() 호출 직전 상태: {get_excel_state()}"
                    )
                    wb = pv.Edit()
                    print(f"[4] Edit() 호출 직후 상태: {get_excel_state()}")
                    break

        # 5. 파일 새로 열기 (가장 핵심 구간)
        if not wb:
            print(f"[5] Workbooks.Open 호출 시도... (비밀번호 인자 전달)")
            print(f"    호출 직전 상태: {get_excel_state()}")

            try:
                # Password 인자를 명시적으로 전달
                wb = excel.Workbooks.Open(
                    Filename=target_path,
                    UpdateLinks=0,
                    ReadOnly=True,
                    Password=password,
                )
                print(f"[5] Workbooks.Open 성공! 호출 직후 상태: {get_excel_state()}")
            except Exception as e:
                print(f"\n[!] Workbooks.Open 실패!")
                print(f"에러 내용: {e}")
                print(
                    "Tip: DisplayAlerts가 False인데도 실패했다면 비밀번호 자체가 틀렸을 확률이 높습니다."
                )
                return

        # 6. 데이터 읽기 테스트
        try:
            ws = wb.Worksheets(sheet_name)
            sample_value = ws.Cells(1, 1).Value
            print(f"[6] 데이터 로드 성공. [1, 1] 셀 값: {sample_value}")
        except Exception as e:
            print(f"[6] 시트 접근 실패: {e}")

    except Exception:
        print("\n" + "=" * 30)
        print("예기치 못한 런타임 에러 발생")
        traceback.print_exc()
        print("=" * 30)

    finally:
        # 7. 자원 정리
        # 특정 PC에서 디버깅 중이라면 이 부분을 주석 처리해서 엑셀이 떠 있는지 눈으로 확인 가능
        if wb:
            wb.Close(SaveChanges=False)
            print("[7] 워크북 닫기 완료.")
        if excel:
            excel.Quit()
            print("[7] 엑셀 프로세스 종료 완료.")

        print("--- 디버깅 종료 ---")


if __name__ == "__main__":
    # 실행 시 파일 경로와 시트명, 비밀번호를 입력하세요.
    path_input = r"\\172.30.1.98\Server\2026\행정\01.2026년 청구서\26년4월\2026년 4월_수정구전체통합명단(new).xlsx"
    sheet_input = "종합_jon(DB)"
    debug_load_excel(Path(path_input), sheet_input, str(3500))
