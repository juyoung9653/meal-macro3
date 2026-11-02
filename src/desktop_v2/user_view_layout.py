"""Share a user's roster appearance inside the same encrypted operating file."""

from .user_messages import user_error
from datetime import datetime
import platform

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from src.domain.view_layout import validate_view_layout
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .theme import application_stylesheet


class UserViewLayoutDialog(QDialog):
    def __init__(self, profiles, parent=None):
        super().__init__(parent)
        self._profiles = profiles
        self.setWindowTitle("다른 사용자 화면 설정 불러오기")
        self.setStyleSheet(application_stylesheet())
        self.resize(660, 460)
        layout = QVBoxLayout(self)
        note = QLabel("사용자를 선택하면 이 PC의 시트 배치와 칼럼 순서·너비·숨김·표시 이름을 바꿉니다. "
                      "명단 데이터와 검색·필터는 유지합니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.users = QComboBox()
        self.users.setObjectName("viewLayoutUser")
        for profile in profiles:
            timestamp = datetime.fromisoformat(profile["updated_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.users.addItem(f'{profile["username"]} · {profile["computer_name"]} · {timestamp}')
        layout.addWidget(self.users)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        if not profiles:
            self.preview.setPlainText("가져올 다른 사용자의 화면 설정이 없습니다.\n"
                "다른 사용자가 이 기능이 있는 버전으로 같은 DB를 열고 종료하면 목록에 나타납니다.")
        buttons = QHBoxLayout()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        self.apply_button = QPushButton("불러오기")
        self.apply_button.setObjectName("primaryAction")
        self.apply_button.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.users.currentIndexChanged.connect(self._update_preview)
        self._update_preview()

    def selected_layout(self):
        index = self.users.currentIndex()
        if not 0 <= index < len(self._profiles):
            raise ValueError("가져올 사용자를 선택해주세요.")
        return validate_view_layout(self._profiles[index]["layout"])

    def _update_preview(self, *_):
        self.apply_button.setEnabled(False)
        if not self._profiles:
            return
        try:
            state = self.selected_layout()
        except (TypeError, ValueError) as error:
            self.preview.setPlainText(user_error(error))
            return
        self.preview.setPlainText("\n".join(
            f'{index}. {sheet["name"]} · 칼럼 {len(sheet["layout"]["order"])}개 · 숨김 {len(sheet["layout"]["hidden"])}개'
            + (" · 선택된 시트" if sheet["id"] == state["active"] else "")
            for index, sheet in enumerate(state["sheets"], 1)))
        self.apply_button.setEnabled(True)


class UserViewLayoutController(QObject):
    def __init__(self, session, window, *, actor=None, computer_name=None,
                 dialog_type=UserViewLayoutDialog, messages=QMessageBox):
        super().__init__(window)
        self.session, self.window = session, window
        self.actor = actor or current_windows_actor()
        self.computer_name = computer_name if computer_name is not None else platform.node()
        self.dialog_type, self.messages = dialog_type, messages
        window.installEventFilter(self)

    def save_current(self):
        return self.session.database.save_user_view_layout(
            self.actor, self.computer_name, self.window.export_view_layout())

    def publish(self):
        try:
            self.save_current()
        except Exception as error:
            self.messages.warning(self.window, "화면 설정 공유 저장 실패",
                "이 PC의 화면 설정은 유지됩니다. 다른 사용자가 가져올 사본을 DB에 저장하지 못했습니다.\n\n" + user_error(error))

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            self.publish()
        return False

    @dialog_workflow
    def run(self):
        try:
            profiles = tuple(profile for profile in self.session.database.user_view_layouts()
                             if profile["sid"] != self.actor.sid)
            dialog = self.dialog_type(profiles, self.window)
            if (yield DialogRequest(dialog)) != QDialog.DialogCode.Accepted:
                return
            self.window.import_view_layout(dialog.selected_layout())
        except Exception as error:
            self.messages.critical(self.window, "화면 설정을 가져오지 못했습니다", user_error(error))
            return
        self.publish()
