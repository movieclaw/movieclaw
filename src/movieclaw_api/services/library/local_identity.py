"""本地来源的识别策略：没有外部身份的文件如何得到标题、内容时间与元数据。

两种调用场景，同一套规则、两个参数不同（docs/design/library-other-kind.md 4.1 / 4.2）：

- 「其他」库（``video`` 形态）：文件名主干原样当标题（家庭视频命名没有
  规律，任何清洗都有误伤），一文件一条目；
- 影视库里 TMDB 认不出的文件（T0 剧集、冷门片）：用识别链已经解析出的
  片名/年份当标题（发布名 ``Show.Name.S01E01.1080p`` → "Show Name" 2024），
  按作品分组——条目目录内的文件共享一条临时条目，散文件按 (片名, 年份)。

无论哪种，视频同名的 sidecar NFO 都**全字段吸收**：大量"不想让 movieclaw
刮削"的内容早被 TMM/番号整理器刮过一遍，NFO 里的标题、简介、演员、封面
原样拿来就是完整展示，而我们一个外部请求都不发。

内容时间的回落链与 Jellyfin 一致：NFO ``premiered``/``aired`` → 容器标签
``date`` → ``creation_time``（手机录像都写）→ 文件 mtime。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from movieclaw_api.services.library.layout import entry_dirs
from movieclaw_api.services.library.nfo import LocalNfo, read_local_sidecar
from movieclaw_api.services.library.resolve import LocalEvidence, normalize_title
from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_media.models import MediaKind


@dataclass
class LocalIdentity:
    """一次本地识别的结论：条目身份 + 可直接落 ``media_metadata`` 的展示字段。"""

    external_id: str  # 本地锚（库内稳定键，见 ``local_external_id``）
    title: str
    year: int | None = None
    sort_title: str | None = None
    plot: str | None = None
    release_date: date | None = None
    runtime_minutes: int | None = None
    rating: float | None = None
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    cast: list[dict] = field(default_factory=list)
    # 身份来源：NFO 给了标题记 NFO，否则记 LOCAL（标题来自文件名/目录名推断）
    identity_source: IdentitySource = IdentitySource.LOCAL

    @property
    def from_nfo(self) -> bool:
        return self.identity_source is IdentitySource.NFO


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def local_external_id(
    library_id: int, kind: MediaKind, root: Path, file: Path, evidence: LocalEvidence | None
) -> str:
    """本地条目的稳定锚（``media_item.external_id``）。

    - ``video`` 形态：一文件一条目，键从库内相对路径派生。同路径再出现
      即复用同一条目（删了再放回、孤儿清理之前），改名/移动由扫描的指纹
      归并保住文件行，条目随之保留；
    - 影视形态（临时身份）：按作品分组——有条目目录取条目目录的相对路径，
      散文件取解析出的 (片名, 年份)。同一部 T0 剧的几十个分集因此共享一条
      临时条目，而不是几十张卡。
    """
    if kind is MediaKind.VIDEO:
        try:
            rel = str(file.relative_to(root))
        except ValueError:
            rel = str(file)
        return f"{library_id}:path:{_digest(rel)}"
    dirs = entry_dirs(root, file)
    if dirs:
        try:
            rel = str(dirs[0].relative_to(root))
        except ValueError:
            rel = str(dirs[0])
        return f"{library_id}:dir:{_digest(rel)}"
    if evidence is not None and evidence.title:
        group = f"{normalize_title(evidence.title)}|{evidence.year or ''}"
        return f"{library_id}:title:{_digest(group)}"
    return f"{library_id}:path:{_digest(str(file))}"


def build_local_identity(
    *,
    library_id: int,
    kind: MediaKind,
    root: Path,
    file: Path,
    spec: MediaSpec | None,
    evidence: LocalEvidence | None = None,
    is_disc: bool = False,
) -> LocalIdentity:
    """（线程池内运行，含磁盘 IO）本地识别：sidecar NFO → 解析证据 → 文件名。"""
    nfo: LocalNfo | None = None
    if not is_disc:
        nfo = read_local_sidecar(file.with_suffix(".nfo"))
    title_from_nfo = bool(nfo and nfo.title)
    if title_from_nfo:
        title = nfo.title  # type: ignore[union-attr]
    elif kind is not MediaKind.VIDEO and evidence is not None and evidence.title:
        title = evidence.title
    else:
        title = file.name if is_disc else file.stem
    title = title.strip() or file.name

    content_date = _content_date(nfo, spec, file)
    year = (nfo.year if nfo and nfo.year else None) or (content_date.year if content_date else None)
    if year is None and kind is not MediaKind.VIDEO and evidence is not None:
        year = evidence.year

    identity = LocalIdentity(
        external_id=local_external_id(library_id, kind, root, file, evidence),
        title=title,
        year=year,
        release_date=content_date,
        identity_source=IdentitySource.NFO if title_from_nfo else IdentitySource.LOCAL,
    )
    if nfo is not None:
        identity.sort_title = nfo.sort_title
        identity.plot = nfo.plot
        identity.runtime_minutes = nfo.runtime_minutes
        identity.rating = nfo.rating
        identity.genres = list(nfo.genres)
        identity.studios = list(nfo.studios)
        identity.directors = list(nfo.directors)
        identity.cast = [
            {
                "name": actor.name,
                "character": actor.role or "",
                "order": index,
                "profile_path": None,
                "thumb": actor.thumb,
                "tmdb_person_id": actor.tmdb_person_id,
            }
            for index, actor in enumerate(nfo.actors)
        ]
    if identity.runtime_minutes is None and spec is not None and spec.duration_seconds:
        identity.runtime_minutes = max(1, round(spec.duration_seconds / 60))
    return identity


def _content_date(nfo: LocalNfo | None, spec: MediaSpec | None, file: Path) -> date | None:
    """内容时间：NFO 日期 → 容器 ``date`` → ``creation_time`` → 文件 mtime。"""
    for raw in (
        nfo.release_date if nfo else None,
        spec.tag_date if spec else None,
        spec.creation_time if spec else None,
    ):
        parsed = _parse_date(raw)
        if parsed is not None:
            return parsed
    try:
        return datetime.fromtimestamp(file.stat().st_mtime, tz=UTC).date()
    except OSError:
        return None


def _parse_date(raw: str | None) -> date | None:
    """宽松解析：``2024-05-01``、``2024-05-01T10:20:30Z``、``2024``、``2024:05:01 10:20:30``。"""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    head = text[:10].replace(":", "-") if len(text) >= 10 and text[4] == ":" else text[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        pass
    if len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        if 1900 <= year <= 2100:
            return date(year, 1, 1)
    return None
