"""Last validated delivery-list template, isolated by operating file and profile."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
import os

from src.excel.delivery_templates import validate_delivery_template


@dataclass(frozen=True)
class TemplateResolution:
    contents: bytes
    used_previous: bool = False
    reason: str | None = None

    @property
    def warning(self):
        if not self.used_previous:
            return None
        return ("수정한 배송목록 양식의 서식이나 구조에 문제가 있어 이전 정상 저장본을 사용했습니다. "
                "수정한 원본 파일은 그대로 두었습니다. 양식 확인/수정에서 내용을 확인해주세요.\n\n" + self.reason)


class DeliveryTemplateBackup:
    def __init__(self, root):
        self.root = Path(root) / "last_valid_substitute"

    def backup_path(self, source):
        identity = str(Path(source).resolve()).casefold().encode("utf-8")
        return self.root / (sha256(identity).hexdigest() + ".xlsx")

    def _validated_copy(self, contents, target=None):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.root, suffix=".xlsx", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
            validate_delivery_template(temporary, "delivery_substitute")
            if target is not None:
                if target.is_file() and target.read_bytes() == contents:
                    return
                os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def remember(self, source):
        contents = Path(source).read_bytes()
        self._validated_copy(contents, self.backup_path(source))
        return contents

    def resolve(self, source):
        try:
            return TemplateResolution(self.remember(source))
        except (ValueError, OSError) as error:
            reason = str(error)
            previous = self.backup_path(source)
            try:
                contents = previous.read_bytes()
                self._validated_copy(contents)
            except (ValueError, OSError) as backup_error:
                raise ValueError("배송목록 양식을 사용할 수 없고 이전 정상 저장본도 없습니다. "
                                 "양식 확인/수정에서 기본 양식 또는 정상 파일을 선택해주세요.\n\n" + reason) from backup_error
            return TemplateResolution(contents, True, reason)
