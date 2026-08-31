"""Plain-language first-run, login, recovery, and backup-restore dialogs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.storage import (
    AutomaticBackupResult,
    BackupFormatError,
    BackupIntegrityError,
    BackupRestoreConflictError,
    CredentialUnlockError,
    DatabaseSession,
    DatabaseUnlockError,
    PasswordPolicyError,
)

from .theme import application_stylesheet, register_application_fonts


_ORGANIZATION = "분당구"
_APPLICATION = "급식 배송 운영 SQLite v2"
_DATABASE_PATH_KEY = "storage/database_path"
_BACKUP_DIRECTORY_KEY = "storage/backup_directory"
_DOCUMENT_OUTPUT_DIRECTORY_KEY = "documents/output_directory"


class DesktopStorageSettings:
    """Stores paths only; passwords and DB keys never enter QSettings."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings(_ORGANIZATION, _APPLICATION)

    @property
    def database_path(self) -> Path | None:
        return _optional_path(self._settings.value(_DATABASE_PATH_KEY, ""))

    @property
    def backup_directory(self) -> Path | None:
        return _optional_path(self._settings.value(_BACKUP_DIRECTORY_KEY, ""))

    @property
    def document_output_directory(self) -> Path | None:
        return _optional_path(
            self._settings.value(_DOCUMENT_OUTPUT_DIRECTORY_KEY, "")
        )

    def save_paths(
        self,
        database_path: str | Path,
        backup_directory: str | Path,
    ) -> None:
        self._settings.setValue(_DATABASE_PATH_KEY, str(Path(database_path).resolve()))
        self._settings.setValue(
            _BACKUP_DIRECTORY_KEY,
            str(Path(backup_directory).resolve()),
        )
        self._settings.sync()

    def save_document_output_directory(self, directory: str | Path) -> None:
        self._settings.setValue(
            _DOCUMENT_OUTPUT_DIRECTORY_KEY,
            str(Path(directory).resolve()),
        )
        self._settings.sync()


class _DatabaseAccessDialog(QDialog):
    def __init__(
        self,
        *,
        window_title: str,
        title: str,
        subtitle: str,
        rail_title: str,
        rail_copy: str,
        route_stops: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("databaseAccessDialog")
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setStyleSheet(application_stylesheet())
        self._session: DatabaseSession | None = None
        self.backup_result = AutomaticBackupResult()

        shell = QHBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(0)
        shell.addWidget(
            self._build_rail(rail_title, rail_copy, route_stops)
        )

        card = QFrame()
        card.setObjectName("startupCard")
        card.setMinimumWidth(470)
        self._content = QVBoxLayout(card)
        self._content.setContentsMargins(30, 26, 30, 26)
        self._content.setSpacing(11)
        heading = QLabel(title)
        heading.setObjectName("startupTitle")
        self._content.addWidget(heading)
        description = QLabel(subtitle)
        description.setObjectName("startupSubtitle")
        description.setWordWrap(True)
        self._content.addWidget(description)
        self._content.addSpacing(5)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        shell.addWidget(card, 1)

    @property
    def session(self) -> DatabaseSession | None:
        return self._session

    def _build_rail(
        self,
        title: str,
        copy: str,
        route_stops: tuple[str, ...],
    ) -> QFrame:
        rail = QFrame()
        rail.setObjectName("startupRail")
        rail.setFixedWidth(218)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(24, 25, 22, 25)
        product = QLabel("MEAL ROUTE · LOCAL")
        product.setObjectName("startupProduct")
        layout.addWidget(product)
        layout.addSpacing(15)
        heading = QLabel(title)
        heading.setObjectName("startupRailTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        explanation = QLabel(copy)
        explanation.setObjectName("startupRailCopy")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addSpacing(25)

        for index, stop in enumerate(route_stops):
            stop_row = QHBoxLayout()
            stop_row.setSpacing(9)
            dot = QLabel("●")
            dot.setObjectName("routeDot")
            dot.setFixedWidth(17)
            stop_row.addWidget(dot)
            label = QLabel(stop)
            label.setObjectName("routeStop")
            label.setWordWrap(True)
            stop_row.addWidget(label, 1)
            layout.addLayout(stop_row)
            if index != len(route_stops) - 1:
                line_row = QHBoxLayout()
                line_row.setContentsMargins(7, 0, 0, 0)
                line = QFrame()
                line.setObjectName("routeLine")
                line.setFixedHeight(18)
                line_row.addWidget(line)
                line_row.addStretch(1)
                layout.addLayout(line_row)
        layout.addStretch(1)
        footer = QLabel("DB · 자동 백업 · 복구")
        footer.setObjectName("startupRailCopy")
        layout.addWidget(footer)
        return rail

    def _add_path_field(
        self,
        label_text: str,
        edit: QLineEdit,
        button_text: str,
        callback,
        *,
        help_text: str | None = None,
    ) -> None:
        self._content.addWidget(_field_label(label_text))
        row = QHBoxLayout()
        row.setSpacing(7)
        edit.setClearButtonEnabled(True)
        row.addWidget(edit, 1)
        browse = QPushButton(button_text)
        browse.setObjectName("browseAction")
        browse.clicked.connect(callback)
        row.addWidget(browse)
        self._content.addLayout(row)
        if help_text:
            self._content.addWidget(_help_label(help_text))

    def _add_password_field(
        self,
        label_text: str,
        edit: QLineEdit,
        *,
        placeholder: str = "",
    ) -> None:
        self._content.addWidget(_field_label(label_text))
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(placeholder)
        self._content.addWidget(edit)

    def _add_show_passwords(self, edits: tuple[QLineEdit, ...]) -> QCheckBox:
        checkbox = QCheckBox("입력한 비밀번호 보기")

        def toggle(checked: bool) -> None:
            mode = (
                QLineEdit.EchoMode.Normal
                if checked
                else QLineEdit.EchoMode.Password
            )
            for edit in edits:
                edit.setEchoMode(mode)

        checkbox.toggled.connect(toggle)
        self._content.addWidget(checkbox)
        return checkbox

    def _add_actions(
        self,
        primary_text: str,
        callback,
        *,
        secondary_text: str = "취소",
    ) -> QPushButton:
        self._content.addWidget(self._error)
        self._content.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(secondary_text)
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        primary = QPushButton(primary_text)
        primary.setObjectName("primaryAction")
        primary.setDefault(True)
        primary.clicked.connect(callback)
        row.addWidget(primary)
        self._content.addLayout(row)
        return primary

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()
        self.adjustSize()

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.hide()


class CreateDatabaseDialog(_DatabaseAccessDialog):
    """One-time setup; ordinary users only see one password afterwards."""

    def __init__(
        self,
        *,
        settings: DesktopStorageSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            window_title="처음 설정",
            title="운영 파일 만들기",
            subtitle=(
                "저장 위치와 자동 백업 위치를 정한 뒤 비밀번호를 설정합니다. "
                "이 설정은 처음 한 번만 하며, 완료하면 기존 Excel 명단을 옮깁니다."
            ),
            rail_title="세 지점만 확인하면 끝납니다",
            rail_copy="배송 경로를 정하듯 저장 위치부터 복구 수단까지 순서대로 준비합니다.",
            route_stops=(
                "운영 DB 저장 위치",
                "평소 사용할 비밀번호",
                "자동 백업과 기관 복구",
            ),
            parent=parent,
        )
        self.resize(790, 720)
        self._settings = settings
        self.database_path_edit = QLineEdit()
        self.database_path_edit.setObjectName("databasePathEdit")
        self.backup_directory_edit = QLineEdit()
        self.backup_directory_edit.setObjectName("backupDirectoryEdit")
        self.user_password_edit = QLineEdit()
        self.user_password_edit.setObjectName("userPasswordEdit")
        self.user_password_confirmation_edit = QLineEdit()
        self.user_password_confirmation_edit.setObjectName("userPasswordConfirmationEdit")
        self.recovery_password_edit = QLineEdit()
        self.recovery_password_edit.setObjectName("recoveryPasswordEdit")
        self.recovery_password_confirmation_edit = QLineEdit()
        self.recovery_password_confirmation_edit.setObjectName(
            "recoveryPasswordConfirmationEdit"
        )

        self._add_path_field(
            "운영 DB 파일",
            self.database_path_edit,
            "위치 선택",
            self._browse_database,
            help_text="명단과 변경 이력이 저장되는 실제 운영 파일입니다.",
        )
        self._add_path_field(
            "자동 백업 폴더",
            self.backup_directory_edit,
            "폴더 선택",
            self._browse_backup_directory,
            help_text=(
                "저장할 때마다 복원 가능한 백업이 추가되고 최신 100개만 "
                "자동으로 보관됩니다."
            ),
        )
        self._add_password_field(
            "평소 사용할 비밀번호",
            self.user_password_edit,
            placeholder="프로그램을 열 때 입력",
        )
        self._add_password_field(
            "평소 비밀번호 다시 입력",
            self.user_password_confirmation_edit,
        )
        self._add_password_field(
            "기관 복구 비밀번호",
            self.recovery_password_edit,
            placeholder="평소에는 사용하지 않음",
        )
        self._add_password_field(
            "기관 복구 비밀번호 다시 입력",
            self.recovery_password_confirmation_edit,
        )
        notice = QLabel(
            "기관 복구 비밀번호는 평소 비밀번호와 다르게 정하고 종이에 적어 "
            "사무실에 보관하세요. 별도 복구 파일은 필요하지 않습니다."
        )
        notice.setObjectName("recoveryNotice")
        notice.setWordWrap(True)
        notice.setMinimumHeight(52)
        self._content.addWidget(notice)
        self._add_show_passwords(
            (
                self.user_password_edit,
                self.user_password_confirmation_edit,
                self.recovery_password_edit,
                self.recovery_password_confirmation_edit,
            )
        )
        self.submit_button = self._add_actions(
            "설정 완료하고 명단 옮기기",
            self._submit,
        )

    def _browse_database(self) -> None:
        start = self.database_path_edit.text().strip()
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "운영 DB 파일 위치",
            start,
            "급식 운영 DB (*.sqlite3)",
        )
        if filename:
            path = Path(filename)
            if path.suffix.casefold() != ".sqlite3":
                path = path.with_suffix(".sqlite3")
            self.database_path_edit.setText(str(path))

    def _browse_backup_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "자동 백업 폴더",
            self.backup_directory_edit.text().strip(),
        )
        if directory:
            self.backup_directory_edit.setText(directory)

    def _submit(self) -> None:
        self._clear_error()
        database_text = self.database_path_edit.text().strip()
        backup_text = self.backup_directory_edit.text().strip()
        if not database_text:
            self._show_error("운영 DB 파일 위치를 선택해주세요.")
            return
        if not backup_text:
            self._show_error("자동 백업 폴더를 선택해주세요.")
            return
        database_path = Path(database_text).resolve()
        backup_directory = Path(backup_text).resolve()
        if not database_path.parent.is_dir():
            self._show_error("운영 DB를 저장할 폴더가 없습니다.")
            return
        if not backup_directory.is_dir():
            self._show_error("선택한 자동 백업 폴더가 없습니다.")
            return
        user_password = self.user_password_edit.text()
        recovery_password = self.recovery_password_edit.text()
        if user_password != self.user_password_confirmation_edit.text():
            self._show_error("평소 비밀번호 두 번의 입력이 서로 다릅니다.")
            return
        if recovery_password != self.recovery_password_confirmation_edit.text():
            self._show_error("기관 복구 비밀번호 두 번의 입력이 서로 다릅니다.")
            return
        try:
            session = DatabaseSession.create(
                database_path,
                user_password,
                recovery_password,
                backup_directory=backup_directory,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self._session = session
        self.backup_result = session.try_backup_now()
        if self._settings is not None:
            self._settings.save_paths(database_path, backup_directory)
        self.accept()


class LoginDialog(_DatabaseAccessDialog):
    def __init__(
        self,
        database_path: str | Path,
        *,
        backup_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._database_path = Path(database_path).resolve()
        self._backup_directory = (
            None if backup_directory is None else Path(backup_directory).resolve()
        )
        super().__init__(
            window_title="급식 배송 운영 열기",
            title="운영 파일 열기",
            subtitle=f"{self._database_path.name}의 평소 비밀번호를 입력해주세요.",
            rail_title="오늘의 운영을 이어갑니다",
            rail_copy="명단과 배송 이력은 이 컴퓨터의 암호화 DB에서 불러옵니다.",
            route_stops=(
                f"운영 파일 · {self._database_path.name}",
                "저장할 때 자동 백업",
                "잊으면 기관 비밀번호로 복구",
            ),
            parent=parent,
        )
        self.resize(720, 455)
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("loginPasswordEdit")
        self._add_password_field(
            "비밀번호",
            self.password_edit,
            placeholder="평소 사용하는 비밀번호",
        )
        self._add_show_passwords((self.password_edit,))
        recovery = QPushButton("비밀번호를 잊었어요")
        recovery.setObjectName("linkAction")
        recovery.clicked.connect(self._open_recovery)
        self._content.addWidget(recovery)
        self.submit_button = self._add_actions("운영 파일 열기", self._submit)

    def _submit(self) -> None:
        self._clear_error()
        try:
            self._session = DatabaseSession.open(
                self._database_path,
                self.password_edit.text(),
                backup_directory=self._backup_directory,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            self.password_edit.selectAll()
            self.password_edit.setFocus()
            return
        self.accept()

    def _open_recovery(self) -> None:
        dialog = RecoveryPasswordDialog(
            self._database_path,
            backup_directory=self._backup_directory,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._session = dialog.session
            self.backup_result = dialog.backup_result
            self.accept()


class RecoveryPasswordDialog(_DatabaseAccessDialog):
    def __init__(
        self,
        database_path: str | Path,
        *,
        backup_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._database_path = Path(database_path).resolve()
        self._backup_directory = (
            None if backup_directory is None else Path(backup_directory).resolve()
        )
        super().__init__(
            window_title="비밀번호 복구",
            title="새 평소 비밀번호 만들기",
            subtitle="기관 복구 비밀번호를 확인한 뒤 평소 비밀번호만 새로 바꿉니다.",
            rail_title="DB 내용은 그대로 유지됩니다",
            rail_copy="명단이나 이력은 다시 만들지 않습니다. 여는 비밀번호만 바뀝니다.",
            route_stops=(
                "기관 복구 비밀번호 확인",
                "새 평소 비밀번호 설정",
                "새 자동 백업 생성",
            ),
            parent=parent,
        )
        self.resize(740, 535)
        self.recovery_password_edit = QLineEdit()
        self.recovery_password_edit.setObjectName("recoveryPasswordEdit")
        self.new_password_edit = QLineEdit()
        self.new_password_edit.setObjectName("newUserPasswordEdit")
        self.new_password_confirmation_edit = QLineEdit()
        self.new_password_confirmation_edit.setObjectName(
            "newUserPasswordConfirmationEdit"
        )
        self._add_password_field(
            "기관 복구 비밀번호",
            self.recovery_password_edit,
        )
        self._add_password_field("새 평소 비밀번호", self.new_password_edit)
        self._add_password_field(
            "새 평소 비밀번호 다시 입력",
            self.new_password_confirmation_edit,
        )
        self._add_show_passwords(
            (
                self.recovery_password_edit,
                self.new_password_edit,
                self.new_password_confirmation_edit,
            )
        )
        self.submit_button = self._add_actions(
            "비밀번호 바꾸고 열기",
            self._submit,
        )

    def _submit(self) -> None:
        self._clear_error()
        if self.new_password_edit.text() != self.new_password_confirmation_edit.text():
            self._show_error("새 평소 비밀번호 두 번의 입력이 서로 다릅니다.")
            return
        try:
            session = DatabaseSession.recover_and_open(
                self._database_path,
                self.recovery_password_edit.text(),
                self.new_password_edit.text(),
                backup_directory=self._backup_directory,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self._session = session
        self.backup_result = session.try_backup_now()
        self.accept()


class RestoreBackupDialog(_DatabaseAccessDialog):
    def __init__(
        self,
        *,
        destination_database_path: str | Path | None = None,
        backup_directory: str | Path | None = None,
        settings: DesktopStorageSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._configured_backup_directory = (
            None if backup_directory is None else Path(backup_directory).resolve()
        )
        self._settings = settings
        super().__init__(
            window_title="자동 백업 복원",
            title="자동 백업에서 복원하기",
            subtitle="백업 파일 하나를 선택하면 명단과 전체 변경 이력을 새 DB로 복원합니다.",
            rail_title="청구서를 다시 입력하지 않아도 됩니다",
            rail_copy="자동 백업에는 암호화 DB와 복구에 필요한 비밀번호 정보가 함께 들어 있습니다.",
            route_stops=(
                "자동 백업 파일 선택",
                "비밀번호 확인",
                "새 운영 DB로 검증 복원",
            ),
            parent=parent,
        )
        self.resize(790, 650)
        self.backup_path_edit = QLineEdit()
        self.backup_path_edit.setObjectName("backupPathEdit")
        self.destination_path_edit = QLineEdit(
            "" if destination_database_path is None else str(destination_database_path)
        )
        self.destination_path_edit.setObjectName("restoreDestinationPathEdit")
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("restorePasswordEdit")
        self.new_password_edit = QLineEdit()
        self.new_password_edit.setObjectName("restoreNewPasswordEdit")
        self.new_password_confirmation_edit = QLineEdit()
        self.new_password_confirmation_edit.setObjectName(
            "restoreNewPasswordConfirmationEdit"
        )

        self._add_path_field(
            "자동 백업 파일",
            self.backup_path_edit,
            "파일 선택",
            self._browse_backup,
        )
        self._add_path_field(
            "복원할 운영 DB 파일",
            self.destination_path_edit,
            "위치 선택",
            self._browse_destination,
            help_text="기존 파일을 덮어쓰지 않고 새 파일로만 복원합니다.",
        )
        self.password_label = _field_label("평소 비밀번호")
        self._content.addWidget(self.password_label)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._content.addWidget(self.password_edit)
        self.forgotten_checkbox = QCheckBox("평소 비밀번호도 잊었습니다")
        self.forgotten_checkbox.toggled.connect(self._toggle_recovery_mode)
        self._content.addWidget(self.forgotten_checkbox)

        self.new_password_container = QWidget()
        new_layout = QVBoxLayout(self.new_password_container)
        new_layout.setContentsMargins(0, 0, 0, 0)
        new_layout.setSpacing(8)
        new_layout.addWidget(_field_label("새 평소 비밀번호"))
        self.new_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        new_layout.addWidget(self.new_password_edit)
        new_layout.addWidget(_field_label("새 평소 비밀번호 다시 입력"))
        self.new_password_confirmation_edit.setEchoMode(QLineEdit.EchoMode.Password)
        new_layout.addWidget(self.new_password_confirmation_edit)
        self.new_password_container.hide()
        self._content.addWidget(self.new_password_container)
        self._add_show_passwords(
            (
                self.password_edit,
                self.new_password_edit,
                self.new_password_confirmation_edit,
            )
        )
        self.submit_button = self._add_actions(
            "백업 복원하고 열기",
            self._submit,
        )

    def _browse_backup(self) -> None:
        start = (
            str(self._configured_backup_directory)
            if self._configured_backup_directory is not None
            else self.backup_path_edit.text().strip()
        )
        filename, _filter = QFileDialog.getOpenFileName(
            self,
            "자동 백업 파일 선택",
            start,
            "급식 DB 자동 백업 (*.mealbackup)",
        )
        if filename:
            self.backup_path_edit.setText(filename)

    def _browse_destination(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "복원할 운영 DB 파일",
            self.destination_path_edit.text().strip(),
            "급식 운영 DB (*.sqlite3)",
        )
        if filename:
            path = Path(filename)
            if path.suffix.casefold() != ".sqlite3":
                path = path.with_suffix(".sqlite3")
            self.destination_path_edit.setText(str(path))

    def _toggle_recovery_mode(self, enabled: bool) -> None:
        self.password_label.setText(
            "기관 복구 비밀번호" if enabled else "평소 비밀번호"
        )
        self.new_password_container.setVisible(enabled)
        self.adjustSize()

    def _submit(self) -> None:
        self._clear_error()
        backup_text = self.backup_path_edit.text().strip()
        destination_text = self.destination_path_edit.text().strip()
        if not backup_text:
            self._show_error("자동 백업 파일을 선택해주세요.")
            return
        if not destination_text:
            self._show_error("복원할 운영 DB 파일 위치를 선택해주세요.")
            return
        backup_path = Path(backup_text).resolve()
        destination = Path(destination_text).resolve()
        backup_directory = self._configured_backup_directory or backup_path.parent
        if self.forgotten_checkbox.isChecked():
            if self.new_password_edit.text() != self.new_password_confirmation_edit.text():
                self._show_error("새 평소 비밀번호 두 번의 입력이 서로 다릅니다.")
                return
            action = lambda: DatabaseSession.restore_and_open_with_recovery_password(
                backup_path,
                destination,
                self.password_edit.text(),
                self.new_password_edit.text(),
                backup_directory=backup_directory,
            )
        else:
            action = lambda: DatabaseSession.restore_and_open_with_user_password(
                backup_path,
                destination,
                self.password_edit.text(),
                backup_directory=backup_directory,
            )
        try:
            self._session = action()
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        if self._settings is not None:
            self._settings.save_paths(destination, backup_directory)
        self.accept()


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _help_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldHelp")
    label.setWordWrap(True)
    return label


def _optional_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).resolve()


def _friendly_error(error: Exception) -> str:
    if isinstance(error, (CredentialUnlockError, DatabaseUnlockError)):
        return "비밀번호가 맞지 않거나 운영 DB가 손상되었습니다. 입력을 확인해주세요."
    if isinstance(error, PasswordPolicyError):
        return str(error)
    if isinstance(error, (BackupFormatError, BackupIntegrityError)):
        return "선택한 자동 백업 파일이 올바르지 않거나 손상되었습니다."
    if isinstance(error, BackupRestoreConflictError):
        return "복원할 위치에 기존 파일이 있습니다. 다른 파일 이름을 선택해주세요."
    message = str(error).strip()
    return message or "작업을 완료하지 못했습니다. 입력한 위치를 확인해주세요."
