"""Plain-language first-run, login, and backup-restore dialogs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
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

from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)
from src.storage.database_backup import (
    BackupFormatError,
    BackupIntegrityError,
    BackupRestoreConflictError,
    restore_backup_with_user_password,
)
from src.storage.database_credentials import (
    CredentialUnlockError,
    PasswordPolicyError,
    credential_path_for,
)
from src.storage.encrypted_database import DatabaseUnlockError

from .theme import application_stylesheet, register_application_fonts


from .portable_settings import DesktopStorageSettings
from src.storage.file_lock import database_lock


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
        self._reservation = None
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

    def release_reservation(self) -> None:
        if self._reservation is not None:
            self._reservation.close()
            self._reservation = None

    def done(self, result: int) -> None:
        if result != QDialog.DialogCode.Accepted:
            try:
                if self._session is not None:
                    self._session.close()
                    self._session = None
            finally:
                self.release_reservation()
        super().done(result)

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
        product = QLabel("급식 배송 관리")
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
        footer = QLabel("데이터 · 자동 백업 · 복구")
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
            rail_copy="저장 위치와 비밀번호를 정하면 자동 백업을 시작합니다.",
            route_stops=(
                "운영 데이터 저장 위치",
                "사용할 비밀번호",
                "자동 백업 위치",
            ),
            parent=parent,
        )
        self.resize(790, 600)
        self._settings = settings
        self.database_path_edit = QLineEdit()
        self.database_path_edit.setObjectName("databasePathEdit")
        self.backup_directory_edit = QLineEdit()
        self.backup_directory_edit.setObjectName("backupDirectoryEdit")
        self.user_password_edit = QLineEdit()
        self.user_password_edit.setObjectName("userPasswordEdit")

        self._add_path_field(
            "운영 데이터 파일",
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
            "비밀번호",
            self.user_password_edit,
            placeholder="프로그램을 열 때 입력",
        )
        self._add_show_passwords((self.user_password_edit,))
        self.submit_button = self._add_actions(
            "설정 완료하고 명단 옮기기",
            self._submit,
        )

    def _browse_database(self) -> None:
        start = self.database_path_edit.text().strip()
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "운영 데이터 파일 위치",
            start,
            "급식 운영 데이터 (*.sqlite3)",
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
            self._show_error("운영 데이터 파일 위치를 선택해주세요.")
            return
        if not backup_text:
            self._show_error("자동 백업 폴더를 선택해주세요.")
            return
        database_path = Path(database_text).resolve()
        backup_directory = Path(backup_text).resolve()
        if not database_path.parent.is_dir():
            self._show_error("운영 데이터를 저장할 폴더가 없습니다.")
            return
        if not backup_directory.is_dir():
            self._show_error("선택한 자동 백업 폴더가 없습니다.")
            return
        user_password = self.user_password_edit.text()
        try:
            session = DatabaseSession.create(
                database_path,
                user_password,
                backup_directory=backup_directory,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self._session = session
        self.backup_result = session.try_backup_now()
        try:
            if self._settings is not None:
                self._settings.save_paths(database_path, backup_directory)
        except Exception as error:
            session.close()
            self._session = None
            self._show_error("운영 파일은 생성됐지만 경로 설정을 저장하지 못했습니다. 파일을 보존했습니다. " + _friendly_error(error))
            return
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
            subtitle=f"{self._database_path.name}의 비밀번호를 입력해주세요.",
            rail_title="오늘의 운영을 이어갑니다",
            rail_copy="명단과 배송 이력은 지정된 암호화 데이터 파일에서 불러옵니다.",
            route_stops=(
                f"운영 파일 · {self._database_path.name}",
                "저장할 때 자동 백업",
                "비밀번호로 파일 열기",
            ),
            parent=parent,
        )
        self.resize(720, 455)
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("loginPasswordEdit")
        self._add_password_field(
            "비밀번호",
            self.password_edit,
            placeholder="비밀번호 입력",
        )
        self._add_show_passwords((self.password_edit,))
        self.submit_button = self._add_actions("운영 파일 열기", self._submit)
        self._reservation = database_lock(self._database_path).acquire()

    def _submit(self) -> None:
        self._clear_error()
        try:
            self._session = DatabaseSession.open(
                self._database_path,
                self.password_edit.text(),
                backup_directory=self._backup_directory,
                _lock=self._reservation,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            self.password_edit.selectAll()
            self.password_edit.setFocus()
            return
        self._reservation = None  # Session now owns the handle.
        self.accept()

def relocate_database(
    session: DatabaseSession,
    destination: str | Path,
    password: str,
    settings: DesktopStorageSettings,
) -> Path:
    """Install a verified snapshot at a new path, then remember it for startup."""
    target = Path(destination).resolve()
    if target == session.path:
        raise ValueError("현재 운영 파일과 다른 위치를 선택해주세요.")
    if target.suffix.casefold() != ".sqlite3":
        raise ValueError("운영 데이터 파일 이름은 .sqlite3로 끝나야 합니다.")
    if not target.parent.is_dir():
        raise ValueError("운영 데이터를 저장할 폴더가 없습니다.")
    lock = database_lock(target).acquire()
    try:
        backup = session.backup_now()
        restore_backup_with_user_password(backup.path, target, password, _lock=lock)
        try:
            settings.save_database_path(target)
        except Exception:
            target.unlink(missing_ok=True)
            credential_path_for(target).unlink(missing_ok=True)
            raise
        session.retain_lock(lock)
    except BaseException:
        lock.close()
        raise
    return target


class DatabaseLocationDialog(_DatabaseAccessDialog):
    """Move the next startup to a new, verified copy of the current DB."""

    def __init__(
        self,
        session: DatabaseSession,
        settings: DesktopStorageSettings,
        parent: QWidget | None = None,
    ) -> None:
        self._source_session = session
        self._settings = settings
        super().__init__(
            window_title="운영 데이터 위치 변경",
            title="운영 데이터 위치 변경",
            subtitle="새 위치에 복사한 뒤 프로그램을 종료합니다. 다음 실행부터 새 파일을 사용합니다.",
            rail_title="데이터 저장 위치",
            rail_copy="현재 운영 파일의 최신 내용을 확인한 뒤 새 위치에 보관합니다.",
            route_stops=("새 파일 위치 선택", "비밀번호로 복사본 확인", "다음 실행부터 새 위치 사용"),
            parent=parent,
        )
        self.resize(820, 540)
        self.setMinimumSize(720, 540)
        self._content.addWidget(_field_label("현재 운영 데이터 파일"))
        self.current_path_edit = QLineEdit(str(session.path))
        self.current_path_edit.setObjectName("currentDatabaseLocationEdit")
        self.current_path_edit.setReadOnly(True)
        self._content.addWidget(self.current_path_edit)
        self.destination_path_edit = QLineEdit()
        self.destination_path_edit.setObjectName("databaseLocationPathEdit")
        self.destination_path_edit.setText(str(session.path.with_name(session.path.stem + "-moved.sqlite3")))
        self._add_path_field(
            "새 운영 데이터 파일 위치",
            self.destination_path_edit,
            "위치 선택",
            self._browse_destination,
            help_text="원본은 보존하며 기존 대상 파일은 덮어쓰지 않습니다. 저장할 폴더를 먼저 만들어주세요.",
        )
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("databaseLocationPasswordEdit")
        self._add_password_field("현재 운영 파일 비밀번호", self.password_edit)
        self._add_show_passwords((self.password_edit,))
        self.submit_button = self._add_actions("위치 변경 후 종료", self._submit)

    def _browse_destination(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "새 운영 데이터 파일 위치",
            self.destination_path_edit.text().strip(),
            "급식 운영 데이터 (*.sqlite3)",
        )
        if filename:
            path = Path(filename)
            if path.suffix.casefold() != ".sqlite3":
                path = path.with_suffix(".sqlite3")
            self.destination_path_edit.setText(str(path))

    def _submit(self) -> None:
        self._clear_error()
        destination = self.destination_path_edit.text().strip()
        if not destination:
            self._show_error("새 운영 데이터 파일 위치를 선택해주세요.")
            return
        try:
            relocate_database(
                self._source_session,
                destination,
                self.password_edit.text(),
                self._settings,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self.accept()


class BackupDirectoryDialog(_DatabaseAccessDialog):
    """Change automatic backups while keeping the operating DB open."""

    def __init__(
        self,
        session: DatabaseSession,
        settings: DesktopStorageSettings,
        parent: QWidget | None = None,
    ) -> None:
        self._source_session = session
        self._settings = settings
        current = session.backup_directory
        if current is None:
            saved = settings.backup_directory
            current = saved if saved is not None and saved.is_dir() else None
        self._current_directory = current
        super().__init__(
            window_title="자동 백업 폴더 변경",
            title="자동 백업 폴더",
            subtitle="새 폴더에 백업을 확인한 뒤 저장합니다. 이후 백업부터 새 폴더를 사용합니다.",
            rail_title="자동 백업 위치",
            rail_copy="기존 백업 파일은 원래 폴더에 그대로 보관합니다.",
            route_stops=("새 폴더 선택", "새 폴더에 백업 확인", "이후 백업 위치 변경"),
            parent=parent,
        )
        self.resize(820, 480)
        self.setMinimumSize(720, 480)
        self._content.addWidget(_field_label("현재 자동 백업 폴더"))
        self.current_directory_edit = QLineEdit(str(current) if current is not None else "설정되지 않음")
        self.current_directory_edit.setObjectName("currentBackupDirectoryEdit")
        self.current_directory_edit.setReadOnly(True)
        self._content.addWidget(self.current_directory_edit)
        self.backup_directory_edit = QLineEdit(str(current) if current is not None else "")
        self.backup_directory_edit.setObjectName("newBackupDirectoryEdit")
        self._add_path_field(
            "새 자동 백업 폴더",
            self.backup_directory_edit,
            "폴더 선택",
            self._browse_directory,
            help_text="이미 만든 폴더를 선택해주세요. 기존 백업과 운영 데이터 파일은 옮기지 않습니다.",
        )
        self.submit_button = self._add_actions("저장", self._submit)

    def _browse_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "새 자동 백업 폴더", self.backup_directory_edit.text().strip(),
        )
        if directory:
            self.backup_directory_edit.setText(directory)

    def _submit(self) -> None:
        self._clear_error()
        chosen = self.backup_directory_edit.text().strip()
        if not chosen:
            self._show_error("새 자동 백업 폴더를 선택해주세요.")
            return
        try:
            target = Path(chosen).resolve()
        except (OSError, ValueError):
            self._show_error("새 자동 백업 폴더 경로가 올바르지 않습니다. 다시 선택해주세요.")
            return
        if self._current_directory is not None and target == self._current_directory.resolve():
            self.accept()
            return
        if not target.is_dir():
            self._show_error("새 자동 백업 폴더가 없습니다. 기존 폴더를 선택해주세요.")
            return
        try:
            replacement = self._source_session.prepare_backup_directory(target)
            self._settings.save_backup_directory(target)
            self._source_session.activate_backup_directory(replacement)
        except OSError as error:
            self._show_error(
                "새 자동 백업 폴더에 백업하거나 설정을 저장하지 못했습니다. "
                "폴더 쓰기 권한과 연결 상태를 확인해주세요. " + _friendly_error(error)
            )
            return
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self.accept()


class DatabasePasswordChangeDialog(_DatabaseAccessDialog):
    """Change the open operating file's login password."""

    def __init__(self, session: DatabaseSession, parent: QWidget | None = None) -> None:
        self._source_session = session
        self.password_changed = False
        super().__init__(
            window_title="운영 파일 비밀번호 변경",
            title="운영 파일 비밀번호 변경",
            subtitle="현재 비밀번호를 확인한 뒤 새 비밀번호로 변경합니다.",
            rail_title="운영 파일 잠금",
            rail_copy="앞으로 운영 파일을 열 때 새 비밀번호를 사용합니다.",
            route_stops=("현재 비밀번호 확인", "새 비밀번호 설정", "백업 비밀번호 갱신"),
            parent=parent,
        )
        self.resize(820, 580)
        self.setMinimumSize(720, 580)
        self.current_password_edit = QLineEdit()
        self.current_password_edit.setObjectName("currentDatabasePasswordEdit")
        self._add_password_field("현재 운영 파일 비밀번호", self.current_password_edit)
        self.new_password_edit = QLineEdit()
        self.new_password_edit.setObjectName("newDatabasePasswordEdit")
        self._add_password_field("새 운영 파일 비밀번호", self.new_password_edit)
        self.confirm_password_edit = QLineEdit()
        self.confirm_password_edit.setObjectName("confirmDatabasePasswordEdit")
        self._add_password_field("새 비밀번호 확인", self.confirm_password_edit)
        self._add_show_passwords((
            self.current_password_edit,
            self.new_password_edit,
            self.confirm_password_edit,
        ))
        self._content.addWidget(_help_label(
            "설정된 백업 폴더의 기존 백업도 새 비밀번호로 바꿉니다. 폴더 밖의 복사본은 기존 비밀번호를 사용합니다."
        ))
        self.submit_button = self._add_actions("비밀번호 변경", self._submit)

    def _submit(self) -> None:
        self._clear_error()
        current = self.current_password_edit.text()
        new = self.new_password_edit.text()
        if not current or not new or not self.confirm_password_edit.text():
            self._show_error("현재 비밀번호와 새 비밀번호, 확인 값을 모두 입력해주세요.")
            return
        if new != self.confirm_password_edit.text():
            self._show_error("새 비밀번호와 확인 값이 다릅니다.")
            return
        try:
            self.backup_result = self._source_session.change_user_password(current, new)
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        self.password_changed = current != new
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
            subtitle="백업 파일 하나를 선택하면 명단과 전체 변경 이력을 새 데이터 파일로 복원합니다.",
            rail_title="청구서를 다시 입력하지 않아도 됩니다",
            rail_copy="자동 백업에는 암호화된 데이터 파일과 복구에 필요한 비밀번호 정보가 함께 들어 있습니다.",
            route_stops=(
                "자동 백업 파일 선택",
                "비밀번호 확인",
                "새 운영 데이터로 검증 복원",
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

        self._add_path_field(
            "자동 백업 파일",
            self.backup_path_edit,
            "파일 선택",
            self._browse_backup,
        )
        self._add_path_field(
            "복원할 운영 데이터 파일",
            self.destination_path_edit,
            "위치 선택",
            self._browse_destination,
            help_text="기존 파일을 덮어쓰지 않고 새 파일로만 복원합니다.",
        )
        self._add_password_field("비밀번호", self.password_edit)
        self._add_show_passwords((self.password_edit,))
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
            "급식 데이터 자동 백업 (*.mealbackup)",
        )
        if filename:
            self.backup_path_edit.setText(filename)

    def _browse_destination(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self,
            "복원할 운영 데이터 파일",
            self.destination_path_edit.text().strip(),
            "급식 운영 데이터 (*.sqlite3)",
        )
        if filename:
            path = Path(filename)
            if path.suffix.casefold() != ".sqlite3":
                path = path.with_suffix(".sqlite3")
            self.destination_path_edit.setText(str(path))

    def _submit(self) -> None:
        self._clear_error()
        backup_text = self.backup_path_edit.text().strip()
        destination_text = self.destination_path_edit.text().strip()
        if not backup_text:
            self._show_error("자동 백업 파일을 선택해주세요.")
            return
        if not destination_text:
            self._show_error("복원할 운영 데이터 파일 위치를 선택해주세요.")
            return
        backup_path = Path(backup_text).resolve()
        destination = Path(destination_text).resolve()
        backup_directory = self._configured_backup_directory or backup_path.parent
        try:
            self._session = DatabaseSession.restore_and_open_with_user_password(
                backup_path,
                destination,
                self.password_edit.text(),
                backup_directory=backup_directory,
            )
        except Exception as error:
            self._show_error(_friendly_error(error))
            return
        try:
            if self._settings is not None:
                self._settings.save_paths(destination, backup_directory)
        except Exception as error:
            self._session.close()
            self._session = None
            self._show_error("운영 파일은 복원됐지만 경로 설정을 저장하지 못했습니다. 파일을 보존했습니다. " + _friendly_error(error))
            return
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


def _friendly_error(error: Exception) -> str:
    if isinstance(error, (CredentialUnlockError, DatabaseUnlockError)):
        return "비밀번호가 맞지 않거나 운영 데이터가 손상되었습니다. 입력을 확인해주세요."
    if isinstance(error, PasswordPolicyError):
        return str(error)
    if isinstance(error, (BackupFormatError, BackupIntegrityError)):
        return "선택한 자동 백업 파일이 올바르지 않거나 손상되었습니다."
    if isinstance(error, BackupRestoreConflictError):
        return "복원할 위치에 기존 파일이 있습니다. 다른 파일 이름을 선택해주세요."
    message = str(error).strip()
    return message or "작업을 완료하지 못했습니다. 입력한 위치를 확인해주세요."
