import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from src.core.delivery_matrix import AnnualDeliveryBook
from src.core.settings import TEMPLATE_PROFILES, get_settings
from src.core.user_preferences import UserPreferences
from src.excel._parsing import parse_annual_date_header
from src.excel.annual_workbook import (
    AnnualWorkbook,
    WorkbookPasswordError,
    WorkbookPasswordRequiredError,
)
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.db_header_mapping_dialog import DbHeaderMappingDialog
from src.widgets.delivery_substitute_tab import DeliverySubstituteTab
from src.widgets.invoice_tab import InvoiceTab
from src.widgets.report_tab import ReportTab
from src.widgets.review_tab import ReviewTab
from src.widgets.roster_merge_tab import RosterMergeTab


class _ToolWindow(QDialog):
    """메인 선택 화면에서 여는 재사용 가능한 비모달 작업 창."""

    def __init__(self, title: str, content: QWidget, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(900, 650)
        self.resize(1000, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._preferences = UserPreferences(self._s)
        self._annual_workbook: AnnualWorkbook | None = None
        self._annual_book: AnnualDeliveryBook | None = None
        self._restore_meal_type()
        self._setup_window()
        self._setup_central()
        self._setup_tabs()
        self._connect_signals()
        self._setup_statusbar()
        self._restore_last_db_file()

    def _setup_window(self):
        self.setWindowTitle(self._s.get("app", "title"))
        w, h = self._s.get_app_window_size()
        self.setMinimumSize(w, h)
        self.resize(w, h)

    def _setup_central(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        # ── 업체명 선택 + db 파일 불러오기 ──
        meal_layout = QHBoxLayout()
        meal_layout.addWidget(QLabel("업체명:"))
        self.meal_combo = QComboBox()
        self.meal_combo.addItems([profile.label for profile in TEMPLATE_PROFILES.values()])
        self.meal_combo.setCurrentText(self._s.get_meal_type())
        self.meal_combo.currentTextChanged.connect(self._on_meal_changed)
        meal_layout.addWidget(self.meal_combo)
        meal_layout.addSpacing(30)
        meal_layout.addWidget(QLabel("db 파일:"))
        self.roster_label = QLabel("db 파일 로드 대기 중")
        self.roster_label.setStyleSheet("color: #888;")
        meal_layout.addWidget(self.roster_label)
        self.roster_btn = QPushButton("db 파일 선택")
        self.roster_btn.clicked.connect(self._select_annual_file)
        meal_layout.addWidget(self.roster_btn)
        self.header_mapping_btn = QPushButton("DB 가져오기 설정")
        self.header_mapping_btn.setEnabled(False)
        self.header_mapping_btn.clicked.connect(self._edit_db_header_mapping)
        meal_layout.addWidget(self.header_mapping_btn)
        meal_layout.addStretch()
        layout.addLayout(meal_layout)

        self.work_group = QGroupBox("작업 선택")
        self.work_group.setStyleSheet(
            """
            QGroupBox {
                background: #f7f9fc;
                border: 1px solid #d9e0ea;
                border-radius: 12px;
                font-size: 17px;
                font-weight: 700;
                margin-top: 14px;
                padding: 18px 14px 14px 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 7px;
                color: #26364a;
            }
            QFrame#workItem {
                background: white;
                border: 1px solid #dce3ec;
                border-radius: 9px;
            }
            QLabel#workNumber {
                background: #e8f0fb;
                color: #285f9f;
                border-radius: 18px;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#workTitle {
                color: #1e2937;
                font-size: 16px;
                font-weight: 700;
            }
            QLabel#workDescription {
                color: #6c7888;
                font-size: 12px;
                font-weight: 400;
            }
            QPushButton#workOpenButton {
                background: #2f6fab;
                color: white;
                border: none;
                border-radius: 7px;
                padding: 9px 18px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#workOpenButton:hover { background: #255e95; }
            QPushButton#workOpenButton:pressed { background: #1f507f; }
            QPushButton#workOpenButton:disabled {
                background: #c7ced8;
                color: #f4f5f7;
            }
            """
        )
        work_layout = QVBoxLayout(self.work_group)
        work_layout.setSpacing(9)
        work_layout.setContentsMargins(10, 12, 10, 10)
        work_definitions = (
            ("청구서 생성", "선택한 월의 급식비 청구서를 만듭니다."),
            ("확인서 생성", "대상자별 급식 확인서를 만듭니다."),
            ("명단 합치기", "최신 한-행 명단을 백업 후 db에 반영합니다."),
            ("검토하기", "db 인적사항 불일치를 별도 Excel로 확인합니다."),
        )
        self.work_titles = tuple(title for title, _description in work_definitions)
        self.work_item_frames: list[QFrame] = []
        buttons: list[QPushButton] = []
        for index, (title, description) in enumerate(work_definitions, start=1):
            item = QFrame()
            item.setObjectName("workItem")
            item.setMinimumHeight(76)
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(14, 10, 14, 10)
            item_layout.setSpacing(14)

            number_label = QLabel(f"{index:02d}")
            number_label.setObjectName("workNumber")
            number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number_label.setFixedSize(36, 36)
            item_layout.addWidget(number_label)

            text_layout = QVBoxLayout()
            text_layout.setSpacing(3)
            title_label = QLabel(title)
            title_label.setObjectName("workTitle")
            description_label = QLabel(description)
            description_label.setObjectName("workDescription")
            text_layout.addWidget(title_label)
            text_layout.addWidget(description_label)
            item_layout.addLayout(text_layout, 1)

            button = QPushButton("열기  ›")
            button.setObjectName("workOpenButton")
            button.setEnabled(False)
            button.setFixedWidth(105)
            item_layout.addWidget(button)
            work_layout.addWidget(item)
            self.work_item_frames.append(item)
            buttons.append(button)

        (
            self.invoice_window_btn,
            self.confirm_window_btn,
            self.merge_window_btn,
            self.review_window_btn,
        ) = buttons
        self.work_buttons = (
            self.invoice_window_btn,
            self.confirm_window_btn,
            self.merge_window_btn,
            self.review_window_btn,
        )
        layout.addWidget(self.work_group)
        layout.addStretch(1)

        self.setCentralWidget(central)

    def _setup_tabs(self):
        meal_type = self._s.get_meal_type()
        self.invoice_tab = InvoiceTab(meal_type=meal_type)
        self.confirm_tab = ConfirmTab(meal_type=meal_type)
        self.report_tab = ReportTab(meal_type=meal_type)
        self.delivery_tab = DeliverySubstituteTab(meal_type=meal_type)
        self.review_tab = ReviewTab(preferences=self._preferences)
        self.merge_tab = RosterMergeTab()
        self.invoice_window = _ToolWindow("청구서 생성", self.invoice_tab, self)
        self.confirm_window = _ToolWindow("확인서 생성", self.confirm_tab, self)
        self.merge_window = _ToolWindow("명단 합치기", self.merge_tab, self)
        self.review_window = _ToolWindow("검토하기", self.review_tab, self)

    def _connect_signals(self):
        self.invoice_window_btn.clicked.connect(self._show_invoice_window)
        self.confirm_window_btn.clicked.connect(self._show_confirm_window)
        self.merge_window_btn.clicked.connect(self._show_merge_window)
        self.review_window_btn.clicked.connect(self._show_review_window)
        self.merge_tab.merge_completed.connect(self._reload_after_merge)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("db 파일을 선택해 주세요.")

    def _restore_meal_type(self):
        """Persisted vendor selection을 복원한다."""
        self._s.set_meal_type(self._preferences.get_meal_type())

    def _on_meal_changed(self, meal_type: str):
        """업체명 변경 → 저장 + in-memory + 양 탭에 전파."""
        meal_type = self._preferences.set_meal_type(meal_type)
        self._s.set_meal_type(meal_type)
        self.invoice_tab.set_meal_type(meal_type)
        self.confirm_tab.set_meal_type(meal_type)
        self.report_tab.set_meal_type(meal_type)
        self.delivery_tab.set_meal_type(meal_type)
        self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

    # ── db 파일 선택 ──

    def _select_annual_file(self):
        configured_path = self._s.get("annual_workbook", "path", default="")
        saved_path = self._preferences.get_annual_workbook_path()
        initial_path = (
            str(self._annual_workbook.path)
            if self._annual_workbook is not None
            else str(saved_path or configured_path or "")
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "db 파일 선택",
            initial_path,
            "Excel 통합 문서 (*.xlsx *.xlsm);;모든 파일 (*.*)",
        )
        if path:
            self._load_annual_file(path, prompt_mapping=True)

    def _load_annual_file(
        self,
        path: str,
        *,
        show_error: bool = True,
        prompt_mapping: bool = False,
        force_mapping_dialog: bool = False,
    ) -> bool:
        """파일을 먼저 검증한 뒤 성공한 장부만 모든 탭에 배포한다."""
        path = os.path.abspath(os.path.expanduser(path))
        password: str | None = None
        prompt_reason: str | None = None
        header_mapping = self._preferences.get_db_header_mapping()
        selected_year = self._preferences.get_db_selected_year()
        cache_headers: tuple[str, ...] | None = None
        if (
            self._annual_workbook is not None
            and os.path.abspath(str(self._annual_workbook.path)) == path
        ):
            header_mapping = self._annual_workbook.header_mapping
            selected_year = self._annual_workbook.selected_year

        while True:
            if prompt_reason is not None:
                password = self._prompt_db_password(
                    path,
                    retry=prompt_reason == "invalid",
                )
                if password is None:
                    self._show_password_cancelled(path)
                    return False

            workbook = AnnualWorkbook(
                path,
                password=password,
                header_mapping=header_mapping,
                selected_year=selected_year,
            )
            password = None
            try:
                table_headers = workbook.table_headers()
                cache_headers = _db_mapping_cache_headers(table_headers)
                cached = self._preferences.get_cached_db_import(cache_headers)
                cached_mapping: dict[str, str] | None = None
                cached_year: int | None = None
                if cached is not None:
                    cached_mapping, raw_cached_year = cached
                    cached_year = _usable_cached_year(
                        raw_cached_year,
                        table_headers,
                    )

                if (
                    not force_mapping_dialog
                    and cached_mapping is not None
                    and cached_year is not None
                ):
                    workbook.header_mapping = cached_mapping
                    workbook.selected_year = cached_year
                elif prompt_mapping or force_mapping_dialog:
                    selection = DbHeaderMappingDialog.get_selection(
                        table_headers,
                        current_mapping=(
                            cached_mapping or workbook.header_mapping
                        ),
                        current_year=(
                            cached_year or workbook.selected_year
                        ),
                        parent=self,
                    )
                    if selection is None:
                        self._show_header_mapping_cancelled(path)
                        return False
                    workbook.header_mapping = selection.mapping
                    workbook.selected_year = selection.year
                book = workbook.load()
            except WorkbookPasswordRequiredError:
                prompt_reason = "required"
                continue
            except WorkbookPasswordError:
                prompt_reason = "invalid"
                continue
            except Exception as error:  # noqa: BLE001 - 사용자용 오류로 표시
                self._show_db_load_error(error, show_error=show_error)
                return False
            finally:
                workbook.forget_password()
            break

        self._annual_workbook = workbook
        self._annual_book = book
        self._s.set("annual_workbook", "path", value=path)

        self._preferences.set_annual_workbook_path(path)
        self._preferences.set_db_header_mapping(workbook.header_mapping)
        self._preferences.set_db_selected_year(book.year)
        if cache_headers is not None:
            self._preferences.set_cached_db_import(
                cache_headers,
                workbook.header_mapping,
                book.year,
            )

        self.invoice_tab.set_annual_book(book)
        self.review_tab.set_annual_book(book)
        self.merge_tab.set_target(path, workbook.header_mapping)
        self.report_tab.set_annual_book(book)
        self.delivery_tab.set_annual_book(book)
        self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

        filename = os.path.basename(path)
        self.roster_label.setText(
            f"✅ {filename} ({book.year}년 · {len(book.roster)}명)"
        )
        self.roster_label.setStyleSheet("color: #0a0; font-weight: bold;")
        self.header_mapping_btn.setEnabled(True)
        for button in self.work_buttons:
            button.setEnabled(True)
        self.statusbar.showMessage(
            f"db 파일 로드 완료: {book.year}년, 명단 {len(book.roster)}명"
        )
        return True

    def _edit_db_header_mapping(self) -> None:
        if self._annual_workbook is None:
            return
        self._load_annual_file(
            str(self._annual_workbook.path),
            prompt_mapping=True,
            force_mapping_dialog=True,
        )

    def _prompt_db_password(self, path: str, *, retry: bool) -> str | None:
        """마스킹 입력값을 반환하되 설정이나 객체 상태에는 저장하지 않는다."""
        filename = os.path.basename(path)
        if retry:
            message = (
                "비밀번호가 올바르지 않습니다.\n"
                f"'{filename}'의 비밀번호를 다시 입력해 주세요."
            )
        else:
            message = f"'{filename}'의 비밀번호를 입력해 주세요."

        while True:
            password, accepted = QInputDialog.getText(
                self,
                "db 파일 비밀번호",
                message,
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return None
            if password:
                return password
            message = "비밀번호를 입력해야 파일을 열 수 있습니다."

    def _show_password_cancelled(self, path: str) -> None:
        filename = os.path.basename(path)
        if self._annual_workbook is not None:
            previous_name = os.path.basename(str(self._annual_workbook.path))
            self.statusbar.showMessage(
                f"암호 입력 취소 · 기존 db 파일 '{previous_name}'을 계속 사용합니다."
            )
            return

        self.roster_label.setText(f"🔒 암호 입력 취소: {filename}")
        self.roster_label.setStyleSheet("color: #b36b00; font-weight: bold;")
        self.statusbar.showMessage("db 파일 암호 입력이 취소되었습니다.")

    def _show_header_mapping_cancelled(self, path: str) -> None:
        filename = os.path.basename(path)
        if self._annual_workbook is not None:
            previous_name = os.path.basename(str(self._annual_workbook.path))
            self.statusbar.showMessage(
                "DB 가져오기 설정 취소 · "
                f"기존 db 파일 '{previous_name}'을 계속 사용합니다."
            )
            return

        self.roster_label.setText(f"DB 가져오기 설정 취소: {filename}")
        self.roster_label.setStyleSheet("color: #b36b00; font-weight: bold;")
        self.statusbar.showMessage("DB 가져오기 설정이 취소되었습니다.")

    def _show_db_load_error(self, error: Exception, *, show_error: bool) -> None:
        if self._annual_workbook is not None:
            previous_name = os.path.basename(str(self._annual_workbook.path))
            retained_message = (
                f"기존 db 파일 '{previous_name}'을 계속 사용합니다."
            )
            self.roster_label.setText(f"⚠️ 로드 실패 · {retained_message}")
            self.roster_label.setStyleSheet(
                "color: #b36b00; font-weight: bold;"
            )
        else:
            retained_message = "현재 활성화된 db 파일이 없습니다."
            self.roster_label.setText(f"⚠️ 로드 실패 · {retained_message}")
            self.roster_label.setStyleSheet("color: #c00;")
        self.statusbar.showMessage(f"db 파일 로드 실패 · {retained_message}")
        if show_error:
            QMessageBox.critical(
                self,
                "db 파일 오류",
                "db 파일을 불러오지 못했습니다:\n"
                f"{error}\n\n{retained_message}",
            )

    def _restore_last_db_file(self) -> bool:
        """마지막으로 정상 로드한 db 파일을 창 생성 시 자동 복원한다."""
        saved_path = self._preferences.get_annual_workbook_path()
        if not saved_path:
            return False

        path = os.path.abspath(os.path.expanduser(str(saved_path)))
        if not os.path.isfile(path):
            filename = os.path.basename(path) or path
            self.roster_label.setText(f"⚠️ 저장된 db 파일 없음: {filename}")
            self.roster_label.setStyleSheet("color: #b36b00; font-weight: bold;")
            self.statusbar.showMessage(
                "저장된 db 파일을 찾을 수 없습니다. 새 db 파일을 선택해 주세요."
            )
            return False

        return self._load_annual_file(path, show_error=False)

    def _show_invoice_window(self) -> None:
        self._sync_output_dirs()
        self._show_tool_window(self.invoice_window, "청구서 생성")

    def _show_confirm_window(self) -> None:
        self._sync_output_dirs()
        self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())
        self._show_tool_window(self.confirm_window, "확인서 생성")

    def _show_merge_window(self) -> None:
        self._show_tool_window(self.merge_window, "명단 합치기")

    def _show_review_window(self) -> None:
        self.review_tab.set_output_dir(self._preferences.get_output_dir())
        self._show_tool_window(self.review_window, "검토하기")

    def _sync_output_dirs(self) -> None:
        output_dir = self._preferences.get_output_dir()
        if output_dir:
            self.invoice_tab.set_output_dir(output_dir)
            self.confirm_tab.set_output_dir(output_dir)

    def _show_tool_window(self, window: QDialog, status: str) -> None:
        window.show()
        window.raise_()
        window.activateWindow()
        self.statusbar.showMessage(status)

    def _reload_after_merge(self, path: str) -> None:
        if self._load_annual_file(path, show_error=True):
            self.statusbar.showMessage("명단 합치기 완료 · db 파일 다시 불러옴")


def _db_mapping_cache_headers(headers: tuple[str, ...]) -> tuple[str, ...]:
    """날짜가 바뀐 같은 DB 구조에서도 매핑 캐시를 재사용한다."""
    return tuple(
        header
        for header in headers
        if parse_annual_date_header(header) is None
    )


def _usable_cached_year(
    cached_year: int,
    headers: tuple[str, ...],
) -> int | None:
    available_years = tuple(
        sorted(
            {
                parsed.year
                for header in headers
                if (parsed := parse_annual_date_header(header)) is not None
            }
        )
    )
    if cached_year in available_years:
        return cached_year
    if len(available_years) == 1:
        return available_years[0]
    return None
