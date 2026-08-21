"""AI 字幕生成 G1（docs/design/subtitle-ai-translate.md §10 验收）。

翻译管线用假 LLM 注入（translate 层与 movieclaw_llm 解耦的直接收益）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import numpy as np
import pytest

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.subtitle_gen import extract, source, sync, tasks, translate, validate
from movieclaw_db.models import LibraryFile

# ---------------------------------------------------------------------------
# 选源打分矩阵（§2）
# ---------------------------------------------------------------------------


def _file(embedded: list[dict] | None, external: list[dict] | None) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/media/Movie.mkv",
        duration_seconds=6000,
        subtitle_streams=embedded,
        external_subtitles=external,
        source="scanned",
    )


def test_rank_excludes_forced_target_and_graphic() -> None:
    f = _file(
        [
            {"codec": "subrip", "language": "eng", "forced": True},
            {"codec": "hdmv_pgs_subtitle", "language": "eng"},
        ],
        [{"filename": "Movie.chs.srt", "format": "srt", "language": "chi"}],
    )
    ranked = source.rank_candidates(f, original_language="en", target_language="chs")
    excluded = {c.candidate.key: c.excluded for c in ranked}
    assert "forced" in excluded["0"]
    assert "图形字幕" in excluded["1"]
    assert "目标语言" in excluded["Movie.chs.srt"]


def test_rank_original_language_beats_english() -> None:
    f = _file(
        [{"codec": "subrip", "language": "eng"}, {"codec": "ass", "language": "jpn"}],
        [],
    )
    ranked = source.rank_candidates(f, original_language="ja", target_language="chs")
    usable = [c for c in ranked if not c.excluded]
    assert usable[0].candidate.language == "jpn"  # 原语言 > 英语


def test_rank_embedded_wins_tie_over_external() -> None:
    # v2.1 评审修正：同为英语时内封（官方字幕概率高）排在外挂前
    f = _file(
        [{"codec": "subrip", "language": "eng"}],
        [{"filename": "Movie.eng.srt", "format": "srt", "language": "eng"}],
    )
    ranked = source.rank_candidates(f, original_language="en", target_language="chs")
    usable = [c for c in ranked if not c.excluded]
    assert usable[0].candidate.kind == "embedded"


def test_rank_prefers_ocr_source_over_ai_generated_sidecars() -> None:
    """同语言外挂默认选最接近原始字幕的一代，AI 成品仍保留为手选项。"""
    row = _file(
        [],
        [
            {
                "filename": "Movie.ai-bilingual-eng-chs.eng.srt",
                "title": "ai-bilingual-eng-chs",
                "format": "srt",
                "language": "eng",
            },
            {
                "filename": "Movie.ai-chs.chi.srt",
                "title": "ai-chs",
                "format": "srt",
                "language": "chi",
            },
            {
                "filename": "Movie.pgs-ocr.eng.srt",
                "title": "pgs-ocr",
                "format": "srt",
                "language": "eng",
            },
        ],
    )

    ranked = source.rank_candidates(row, original_language="fre", target_language="jpn")
    usable = [candidate for candidate in ranked if candidate.excluded is None]
    english = [candidate for candidate in usable if candidate.candidate.language == "eng"]

    assert usable[0].candidate.key == "Movie.pgs-ocr.eng.srt"
    assert usable[0].candidate.provenance == "pgs_ocr"
    assert english[-1].candidate.provenance == "ai_bilingual"


def test_default_reference_prefers_english_over_original_language() -> None:
    """确认弹窗默认英语，但没有英语时仍沿用原语言优先的质量排序。"""
    row = _file(
        [
            {"codec": "subrip", "language": "jpn"},
            {"codec": "subrip", "language": "eng"},
        ],
        [],
    )
    ranked = source.rank_candidates(row, original_language="jpn", target_language="chs")

    selected = tasks._select_reference(ranked, None)

    assert selected is not None
    assert tasks.candidate_key(selected) == "embedded:1"


def test_reference_can_explicitly_select_pgs() -> None:
    row = _file(
        [
            {"codec": "subrip", "language": "eng"},
            {"codec": "hdmv_pgs_subtitle", "language": "jpn"},
        ],
        [],
    )
    ranked = source.rank_candidates(row, original_language="jpn", target_language="chs")

    selected = tasks._select_reference(ranked, "embedded:1")

    assert selected is not None
    assert selected.exclusion_code == "pgs"
    assert tasks.candidate_selectable(selected)


def test_reference_rejects_stale_or_unsupported_candidate() -> None:
    row = _file([{"codec": "dvd_subtitle", "language": "eng"}], [])
    ranked = source.rank_candidates(row, original_language="eng", target_language="chs")

    with pytest.raises(BadRequestException, match="不能用于翻译"):
        tasks._select_reference(ranked, "embedded:0")
    with pytest.raises(BadRequestException, match="已不存在"):
        tasks._select_reference(ranked, "embedded:9")


async def test_source_fingerprint_detects_external_subtitle_replacement(tmp_path) -> None:
    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.eng.srt"
    video.write_bytes(b"video")
    subtitle.write_text("first", encoding="utf-8")
    stat = subtitle.stat()
    row = _file(
        [],
        [
            {
                "filename": subtitle.name,
                "format": "srt",
                "language": "eng",
                "size_bytes": stat.st_size,
                "file_mtime_ns": stat.st_mtime_ns,
            }
        ],
    )
    row.file_path = str(video)

    before = await tasks._source_fingerprint(row, f"external:{subtitle.name}")
    subtitle.write_text("replacement with different contents", encoding="utf-8")
    after = await tasks._source_fingerprint(row, f"external:{subtitle.name}")

    assert before != after


async def test_preview_loads_only_the_requested_reference(monkeypatch) -> None:
    row = _file(
        [
            {"codec": "subrip", "language": "jpn"},
            {"codec": "subrip", "language": "eng"},
        ],
        [],
    )
    loaded: list[str] = []
    complete = [(i * 100_000, i * 100_000 + 2000, f"line {i}") for i in range(60)]

    async def fake_load_row(_session, _file_id):  # noqa: ANN001
        return row

    async def fake_context(_session, _row):  # noqa: ANN001
        return CTX, "jpn"

    async def fake_load(_row, candidate):  # noqa: ANN001
        loaded.append(candidate.key)
        return complete

    monkeypatch.setattr(tasks, "_load_row", fake_load_row)
    monkeypatch.setattr(tasks, "_film_context", fake_context)
    monkeypatch.setattr(extract, "load_candidate_events", fake_load)

    result = await tasks.preview(
        None,  # type: ignore[arg-type]
        7,
        "chs",
        source_candidate_key="embedded:0",
    )

    assert loaded == ["0"]
    assert result.selected_source_key == "embedded:0"
    assert result.chosen is not None and result.chosen.candidate.language == "jpn"


def test_assess_completeness() -> None:
    few = [(i * 1000, i * 1000 + 500, "x") for i in range(10)]
    assert not source.assess_events(few, 6000).ok  # 条数太少
    fragment = [(i * 1000, i * 1000 + 500, "x") for i in range(60)]
    assert not source.assess_events(fragment, 6000).ok  # 只覆盖开头 1 分钟
    full = [(i * 60_000, i * 60_000 + 2000, "x") for i in range(90)]
    assert source.assess_events(full, 6000).ok


def test_preview_blocker_explains_graphic_subtitles() -> None:
    """图片字幕不是“没有字幕”，预检要用普通用户能理解的方式说明下一步。"""
    file = _file(
        [
            {"codec": "hdmv_pgs_subtitle", "language": "eng"},
            {"codec": "hdmv_pgs_subtitle", "language": "spa"},
        ],
        [],
    )
    ranked = source.rank_candidates(file, original_language="fre", target_language="chs")

    blocker = tasks._preview_blocker(ranked)

    assert blocker.code == "graphics_only"
    assert "2 条" in blocker.message
    assert "图片字幕" in blocker.message
    assert "PGS" not in blocker.message and "OCR" not in blocker.message
    assert any("SRT" in suggestion for suggestion in blocker.suggestions)


def test_preview_blocker_explains_missing_subtitles() -> None:
    blocker = tasks._preview_blocker([])

    assert blocker.code == "no_subtitle"
    assert "不会从音轨听写" in blocker.message


# ---------------------------------------------------------------------------
# 机检（§3.3/§3.5：折行/标点/CPS）
# ---------------------------------------------------------------------------


def test_clean_punctuation_rules() -> None:
    assert validate.clean_punctuation("你好，世界。") == "你好 世界"
    # ？！/！！ 组合是 Netflix 禁用项，收敛为首个标点
    assert validate.clean_punctuation("真的吗？！！") == "真的吗？"
    assert validate.clean_punctuation("１９８４年") == "1984年"
    assert validate.clean_punctuation("等等…") == "等等…"  # 省略号保留


def test_non_chinese_output_keeps_its_own_punctuation_and_line_width() -> None:
    text = "This sentence, unlike Chinese subtitles, keeps its punctuation."

    out, report = validate.finalize_events([(0, 5000, text)], target_language="eng")

    assert "," in out[0][2] and out[0][2].endswith("punctuation.")
    assert all(len(line) <= 42 for line in out[0][2].splitlines())
    assert report.cps_overrun == 0


def test_bilingual_quality_checks_each_language_line_separately() -> None:
    events = [(0, 2000, "一句中文\nA concise English line.")]

    out, report = validate.finalize_events(
        events,
        target_language="chs",
        secondary_language="eng",
    )

    assert out[0][2] == "一句中文\nA concise English line."
    assert report.cps_overrun == 0


def test_fold_line_at_sixteen() -> None:
    text = "这是一句明显超过十六个字上限的很长很长的台词"
    folded = validate.fold_line(text)
    lines = folded.split("\n")
    assert len(lines) == 2
    assert len(lines[0]) <= 16


def test_fold_line_short_untouched() -> None:
    assert validate.fold_line("短句") == "短句"


def test_finalize_reports_cps_overrun() -> None:
    events = [(0, 1000, "这句话在一秒内绝对读不完所以必超读速上限")]  # 1s 内 20 字
    out, report = validate.finalize_events(events)
    assert report.cps_overrun == 1
    assert report.event_count == 1


def test_looks_translated_detects_untranslated() -> None:
    assert validate.looks_translated("你好", "chs")
    assert validate.looks_translated("OK", "chs")  # 短英文放过
    assert not validate.looks_translated("This is untranslated english", "chs")


# ---------------------------------------------------------------------------
# 翻译管线（§3：假 LLM、结构校验、断点续传、熔断）
# ---------------------------------------------------------------------------

CTX = translate.FilmContext(title="测试影片", year=2024, genres=["剧情"], overview="简介")


def _events(n: int) -> list[extract.SubEvent]:
    return [(i * 2000, i * 2000 + 1500, f"line {i}") for i in range(n)]


def _prompt_block(user: str) -> list[dict]:
    """只解析待翻译 payload，忽略并发窗口附带的上文原文。"""
    return json.loads(user.split("翻译下列对白：\n", 1)[1])


def _good_chat(monkeypatch=None):
    calls = {"n": 0}

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        calls["n"] += 1
        if "找出其中反复出现的人名" in user:
            return '[{"src": "John", "dst": "约翰"}]'
        block = _prompt_block(user)
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    return chat, calls


async def test_subtitle_chat_uses_fast_kimi_json_mode_and_records_usage(monkeypatch) -> None:
    """字幕专用调用关闭 Kimi 思考、限制输出，并把用量归集到 Job 快照。"""
    from movieclaw_api.services import llm_config
    from movieclaw_llm import ChatResponse, LlmProviderConfig, TokenUsage

    requests = []
    provider = LlmProviderConfig(
        name="Kimi 官方（月之暗面）",
        provider_type="kimi",
        api_key="sk-test",
        default_model="kimi-k2.6",
    )

    class Router:
        def resolve(self, _model_ref):  # noqa: ANN001
            return provider, "kimi-k2.6"

        async def chat(self, request):  # noqa: ANN001
            requests.append(request)
            return ChatResponse(
                content='{"items": []}',
                finish_reason="stop",
                model="kimi-k2.6",
                usage=TokenUsage(
                    prompt_tokens=120,
                    completion_tokens=80,
                    total_tokens=200,
                    cache_read_tokens=20,
                ),
            )

    async def acquire(_session):  # noqa: ANN001
        return Router()

    monkeypatch.setattr(llm_config, "acquire_llm_router", acquire)
    usage = tasks.SubtitleLlmUsage()
    chat = await tasks._build_chat(
        None,
        model_ref="Kimi 官方（月之暗面）/kimi-k2.6",
        job_id="job_test",
        usage=usage,
    )

    result = await chat(
        "system",
        "user",
        translate.ChatCall(purpose="translate", block_index=3, event_count=50),
    )

    assert result == '{"items": []}'
    assert requests[0].settings.extra_body == {"thinking": {"type": "disabled"}}
    assert requests[0].settings.response_format == "json_object"
    # 不自设输出上限：截断的响应照样全额计费却零产出，上限护不住钱包
    assert requests[0].settings.max_tokens is None
    snapshot = usage.snapshot()
    assert snapshot["request_count"] == 1
    assert snapshot["total_tokens"] == 200
    assert snapshot["last_call"]["block_index"] == 3


async def test_translate_happy_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # cache_dir 是相对路径,隔离到临时目录
    chat, calls = _good_chat()
    events = _events(120)  # 3 块
    out, stats = await translate.translate_events(chat, events, CTX, "chs", file_id=1)
    assert len(out) == 120
    assert out[0][2] == "译0" and out[119][2] == "译119"
    assert out[5][0] == events[5][0]  # 时间轴原样
    assert stats.total_blocks == 3 and stats.failed_blocks == 0
    assert stats.glossary == {"John": "约翰"}
    # 断点在翻译层保留（写盘成功后由 tasks 层清理——写盘失败时重跑可全量续传）
    ckpt = json.loads(
        next((tmp_path / "data").rglob("*.checkpoint.json")).read_text(encoding="utf-8")
    )
    assert len(ckpt["blocks"]) == 3


async def test_translate_bilingual_keeps_selected_line_order(tmp_path: Path, monkeypatch) -> None:
    """双语由同一次模型调用生成，且每条严格保持“第一语言 + 第二语言”两行。"""
    monkeypatch.chdir(tmp_path)
    systems: list[str] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        systems.append(system)
        block = _prompt_block(user)
        return json.dumps(
            [{"i": event["i"], "t": f"译{event['i']}\nEnglish {event['i']}"} for event in block]
        )

    out, stats = await translate.translate_events(
        chat,
        _events(60),
        CTX,
        "chs",
        file_id=33,
        secondary_language="eng",
    )

    assert stats.failed_blocks == 0
    assert out[0][2] == "译0\nEnglish 0"
    assert "第一行只写简体中文，第二行只写英语" in systems[0]
    assert next((tmp_path / "data").rglob("*chs-eng*.checkpoint.json")).is_file()


def test_standardized_sidecar_names_are_player_compatible() -> None:
    """末段保留播放器识别的语言码，前段承载 AI 类型与双语组合。"""
    row = _file([], [])

    assert tasks._sidecar_path(row, "chs").name == "Movie.ai-chs.chi.srt"
    assert tasks._sidecar_path(row, "eng").name == "Movie.ai.eng.srt"
    assert tasks._legacy_sidecar_path(row, "chs").name == "Movie.chs.ai.srt"
    assert tasks._sidecar_path(row, "chs", "eng").name == "Movie.ai-bilingual-chs-eng.chi.srt"
    assert tasks._pgs_sidecar_path(row, "eng").name == "Movie.pgs-ocr.eng.srt"
    with pytest.raises(BadRequestException, match="不能选择同一种语言"):
        tasks.ensure_output_languages("chs", "chs")


async def test_translate_block_retry_then_keep_original(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        return "垃圾输出不是 JSON"

    events = _events(40)  # 1 块;全失败→保留原文,1/1 块失败率 100% > 熔断阈值
    with pytest.raises(translate.TranslationAborted):
        await translate.translate_events(chat, events, CTX, "chs", file_id=2)


async def test_translate_checkpoint_resume(tmp_path: Path, monkeypatch) -> None:
    """断点续传：第二次运行不重译已完成块（§3.6：已花的钱不能作废）。"""
    monkeypatch.chdir(tmp_path)
    events = _events(100)  # 2 块
    aborted = {"hit": False}

    async def chat_first(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        if block[0]["i"] >= 50:  # 第二块：模拟中断
            aborted["hit"] = True
            raise RuntimeError("网络中断")
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    with pytest.raises(RuntimeError):
        await translate.translate_events(chat_first, events, CTX, "chs", file_id=3)
    assert aborted["hit"]

    block_calls = {"n": 0}

    async def chat_second(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            raise AssertionError("术语表也该从断点恢复,不该再调")
        block_calls["n"] += 1
        block = _prompt_block(user)
        assert block[0]["i"] == 50  # 只翻第二块
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(chat_second, events, CTX, "chs", file_id=3)
    assert block_calls["n"] == 1
    assert len(out) == 100 and out[99][2] == "译99"


async def test_translate_checkpoint_cannot_bypass_failure_fuse(tmp_path: Path, monkeypatch) -> None:
    """失败块跨重跑累计，不能靠反复续传把整片原文洗成成功结果。"""
    monkeypatch.chdir(tmp_path)

    async def bad_chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        return "不是 JSON"

    events = _events(500)  # 10 块；每轮失败率都必须包含断点里的历史失败块
    with pytest.raises(translate.TranslationAborted):
        await translate.translate_events(bad_chat, events, CTX, "chs", file_id=30)

    # 模拟修复前已经落盘的旧格式断点：升级后也必须识别历史原文回退块。
    checkpoint_path = next((tmp_path / "data").rglob("*.checkpoint.json"))
    legacy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy.pop("failed_blocks")
    checkpoint_path.write_text(json.dumps(legacy), encoding="utf-8")
    for _ in range(4):
        with pytest.raises(translate.TranslationAborted):
            await translate.translate_events(bad_chat, events, CTX, "chs", file_id=30)

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    # 有界并发允许同一批调用一起完成，因此熔断最多产生一个并发窗口的超调。
    assert checkpoint["failed_blocks"] == list(range(translate.TRANSLATION_CONCURRENCY))


async def test_translate_resume_retries_failed_blocks(tmp_path: Path, monkeypatch) -> None:
    """换好模型后续传必须重译失败块，而不是一开跑就被历史失败数顶穿熔断。

    issue #194：失败块存的是保留原文的占位，旧实现把它们当成已完成块跳过，
    累计失败数又直接触发熔断 —— 任务永远停在「重试 22 秒即失败」。
    """
    monkeypatch.chdir(tmp_path)
    events = _events(500)  # 10 块，熔断阈值 20% → 3 块起跳

    async def flaky_chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        if block[0]["i"] < 250:  # 前 5 块坏掉，足以触发熔断
            return "不是 JSON"
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    with pytest.raises(translate.TranslationAborted):
        await translate.translate_events(flaky_chat, events, CTX, "chs", file_id=40)
    checkpoint_path = next((tmp_path / "data").rglob("*.checkpoint.json"))
    failed = json.loads(checkpoint_path.read_text(encoding="utf-8"))["failed_blocks"]
    assert failed, "熔断前应已把失败块记进断点"

    retried: list[int] = []

    async def fixed_chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            raise AssertionError("术语表该从断点恢复")
        block = _prompt_block(user)
        retried.append(block[0]["i"] // 50)
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(fixed_chat, events, CTX, "chs", file_id=40)
    assert set(failed) <= set(retried), "断点里的失败块必须被重新翻译"
    assert stats.failed_blocks == 0 and stats.kept_original == 0
    assert out[0][2] == "译0" and out[499][2] == "译499"
    # 失败名单已销账：再续传不会白白重译已经成功的块
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["failed_blocks"] == []


async def test_translate_splits_block_when_output_truncated(tmp_path: Path, monkeypatch) -> None:
    """撞上模型自身输出上限时立刻拆块，不浪费同尺寸重试。

    管线不自设 max_tokens，截断即意味着撞的是供应商天花板 —— 那是确定性的，
    同样条数重发必然再次截断，所以不该把块重试次数烧在原尺寸上。
    """
    monkeypatch.chdir(tmp_path)
    sizes: list[int] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        sizes.append(len(block))
        if len(block) > 25:  # 整块写不完，半块才写得下
            raise translate.ChatOutputTruncated("撞上模型输出上限")
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(chat, _events(50), CTX, "chs", file_id=41)

    assert stats.failed_blocks == 0
    assert len(out) == 50 and out[0][2] == "译0" and out[49][2] == "译49"
    assert sizes == [50, 25, 25], "截断后应立刻拆块，而不是原尺寸再试两次"


async def test_translate_split_recurses_within_depth_cap(tmp_path: Path, monkeypatch) -> None:
    """供应商天花板很低时逐层再拆，但调用次数有硬上限。"""
    monkeypatch.chdir(tmp_path)
    sizes: list[int] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        sizes.append(len(block))
        if len(block) > 13:  # 只有拆两层后才写得下
            raise translate.ChatOutputTruncated("撞上模型输出上限")
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(chat, _events(50), CTX, "chs", file_id=42)

    assert stats.failed_blocks == 0 and len(out) == 50
    # 50 → 25+25 → 各自 12+13，调用次数落在 1+2+4=7 次的封顶内
    assert sizes == [50, 25, 12, 13, 25, 12, 13]
    assert len(sizes) <= 1 + 2 + 4


async def test_translate_gives_up_when_split_cap_exhausted(tmp_path: Path, monkeypatch) -> None:
    """拆到底仍写不完就认输保留原文，不无限拆下去。"""
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        calls["n"] += 1
        raise translate.ChatOutputTruncated("再怎么拆都写不完")

    with pytest.raises(translate.TranslationAborted):  # 1/1 失败率触发熔断
        await translate.translate_events(chat, _events(50), CTX, "chs", file_id=43)
    assert calls["n"] <= 1 + 2 + 4, "拆分深度必须有硬上限"


def test_validate_block_rejects_non_integer_index() -> None:
    """序号写成 null/数组是「结构不对」，要抛 ValueError 走块重试。

    放任 TypeError 逃出去，一次畸形响应就会打死整个任务 —— 而块重试机制
    存在的意义正是兜住模型的畸形输出。
    """
    block = [(0, "hello there my friend"), (1, "second line here ok")]
    for raw in (
        '[{"i": null, "t": "译0"}, {"i": 1, "t": "译1"}]',
        '[{"i": [0], "t": "译0"}, {"i": 1, "t": "译1"}]',
    ):
        with pytest.raises(ValueError, match="不是整数"):
            translate._validate_block(raw, block, "chs")


def test_checkpoint_save_is_atomic(tmp_path: Path, monkeypatch) -> None:
    """写盘中途崩溃不能毁掉已有断点 —— 那正是断点存在的理由。"""
    monkeypatch.chdir(tmp_path)
    checkpoint = translate.Checkpoint(7, "chs", "fp")
    checkpoint.blocks = {0: ["译0"]}
    checkpoint.save()

    real_write_text = Path.write_text

    def crash(self, *args, **kwargs):  # noqa: ANN001, ANN202
        real_write_text(self, *args, **kwargs)  # 临时文件已写出
        raise OSError("磁盘写满")

    monkeypatch.setattr(Path, "write_text", crash)
    checkpoint.blocks[1] = ["译1"]
    with pytest.raises(OSError):
        checkpoint.save()
    monkeypatch.setattr(Path, "write_text", real_write_text)  # 只撤销这一项，保留 chdir

    reloaded = translate.Checkpoint(7, "chs", "fp")
    reloaded.load()
    assert reloaded.blocks == {0: ["译0"]}, "半截写入不能污染已落盘的断点"


async def test_compress_overruns_keeps_glossary(tmp_path: Path, monkeypatch) -> None:
    """二次压缩是对译文的改写，丢了术语表会让人名地名在这批行上跑偏。"""
    monkeypatch.chdir(tmp_path)
    systems: list[str] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        systems.append(system)
        return '{"items": []}'

    events = [(0, 1000, "这一句明显读不完必须压缩改写才行的超长译文")]
    await translate.compress_overruns(
        chat, events, [0], CTX, "chs", None, glossary={"John": "约翰"}
    )

    assert systems and "John→约翰" in systems[0]


async def test_translate_untranslated_block_detected(tmp_path: Path, monkeypatch) -> None:
    """漏翻检测：整块返回英文原文 → 校验不过 → 重试耗尽保留原文计失败。"""
    monkeypatch.chdir(tmp_path)

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        return json.dumps(
            [{"i": e["i"], "t": "This line was not translated at all"} for e in block]
        )

    events = _events(40)
    with pytest.raises(translate.TranslationAborted):  # 1/1 失败率触发熔断
        await translate.translate_events(chat, events, CTX, "chs", file_id=4)


async def test_translate_uses_bounded_concurrency_and_reports_progress(
    tmp_path: Path, monkeypatch
) -> None:
    """单片并发不超过初始上限，进度同时报告活动块和对白完成数。"""
    monkeypatch.chdir(tmp_path)
    active = 0
    peak = 0
    snapshots: list[translate.TranslationProgress] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        nonlocal active, peak
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    events = _events(500)  # 10 块，足以观察首批八路和第二批两路
    out, stats = await translate.translate_events(
        chat,
        events,
        CTX,
        "chs",
        file_id=31,
        progress=snapshots.append,
    )

    assert peak == translate.TRANSLATION_CONCURRENCY == 8
    assert any(len(snapshot.active_blocks) == 8 for snapshot in snapshots)
    assert snapshots[-1].done_blocks == stats.total_blocks == 10
    assert snapshots[-1].done_events == snapshots[-1].total_events == 500
    assert len(out) == 500


async def test_translate_reports_heartbeat_before_first_block_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    """慢模型首批未返回时也持续发布活动快照，避免页面看起来卡死。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(translate, "_PROGRESS_HEARTBEAT_SECONDS", 0.01)
    snapshots: list[translate.TranslationProgress] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        await asyncio.sleep(0.035)
        return json.dumps([{"i": item["i"], "t": f"译{item['i']}"} for item in block])

    await translate.translate_events(
        chat,
        _events(50),
        CTX,
        "chs",
        file_id=34,
        progress=snapshots.append,
    )

    waiting = [snapshot for snapshot in snapshots if snapshot.done_blocks == 0]
    assert len(waiting) >= 3
    assert all(snapshot.active_blocks == (1,) for snapshot in waiting[1:])


async def test_translate_rate_limit_reduces_concurrency_and_retries(
    tmp_path: Path, monkeypatch
) -> None:
    """429 不应直接废任务：并发减半，退避后重试原块并保留已完成结果。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(translate, "_RATE_LIMIT_MIN_DELAY", 0.01)
    attempts: dict[int, int] = {}
    snapshots: list[translate.TranslationProgress] = []
    messages: list[str] = []

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = _prompt_block(user)
        bi = block[0]["i"] // translate.BLOCK_SIZE
        attempts[bi] = attempts.get(bi, 0) + 1
        if bi == 0 and attempts[bi] == 1:
            raise translate.ChatRateLimited(0.01)
        await asyncio.sleep(0.01)
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(
        chat,
        _events(500),
        CTX,
        "chs",
        file_id=32,
        progress=snapshots.append,
        phase=lambda _phase, message: messages.append(message),
    )

    assert len(out) == 500 and stats.failed_blocks == 0
    assert attempts[0] == 2
    assert any(snapshot.concurrency == 4 for snapshot in snapshots)
    assert any("已降至 4 路" in message for message in messages)


# ---------------------------------------------------------------------------
# L1 同步度（§5.2：能量 VAD + 事件命中率;人造错位样本）
# ---------------------------------------------------------------------------


def _speechy_pcm(intervals_ms: list[tuple[int, int]], total_ms: int) -> np.ndarray:
    """人造音频：语音段为响亮噪声,其余近静音。"""
    rng = np.random.default_rng(seed=7)
    pcm = rng.normal(0, 0.002, int(sync.SAMPLE_RATE * total_ms / 1000)).astype(np.float32)
    for s, e in intervals_ms:
        a, b = int(s / 1000 * sync.SAMPLE_RATE), int(e / 1000 * sync.SAMPLE_RATE)
        pcm[a:b] = rng.normal(0, 0.3, b - a).astype(np.float32)
    return pcm


def test_sync_hit_rate_aligned_vs_shifted() -> None:
    # 稀疏语音（每 10 秒说 2 秒）：错位后事件才会真正落进静音带
    speech_at = [(1000 + i * 10_000, 3000 + i * 10_000) for i in range(6)]  # 60 秒窗
    pcm = _speechy_pcm(speech_at, 60_000)
    speech = sync.speech_intervals(pcm)

    aligned = sync.event_hit_rate(speech, [(s, e) for s, e in speech_at])
    assert aligned is not None and aligned > 0.9
    # 人造 +4 秒错位（落进静音带）:命中率显著下降（§10 验收:人造偏移样本低于阈值）
    shifted = sync.event_hit_rate(speech, [(s + 4000, e + 4000) for s, e in speech_at[:-1]])
    assert shifted is not None and shifted < sync.SYNC_THRESHOLD


def test_sync_returns_none_when_undetectable() -> None:
    assert sync.event_hit_rate([], []) is None


async def test_sample_sync_score_degrades_without_ffmpeg(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sync, "_ffmpeg", lambda: None)
    score = await sync.sample_sync_score(tmp_path / "x.mkv", _events(100), 6000)
    assert score is None  # 无法检测=未知,不误判为不同步


# ---------------------------------------------------------------------------
# 加载层与分层守护
# ---------------------------------------------------------------------------


def test_parse_events_and_sdh_strip(tmp_path: Path) -> None:
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n[door slams] Hello\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n[music]\n"
    )
    events = extract.parse_events(srt, "test")
    assert len(events) == 2
    cleaned = extract.strip_sdh_markers(events)
    assert cleaned == [(1000, 2000, "Hello")]  # 纯音效行整条剔除


def test_gbk_external_sidecar_decoded(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.chs.srt"
    sub.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n中文字幕\n".encode("gbk"))
    text = extract._decode_text(sub.read_bytes(), str(sub))
    assert "中文字幕" in text


def test_subtitle_gen_never_imports_playback_layers() -> None:
    import ast

    pkg = Path(__file__).resolve().parents[2] / "src/movieclaw_api/services/subtitle_gen"
    for py in pkg.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("movieclaw_jellyfin", "movieclaw_playback")), (
                    f"生产端文件 {py.name} import 了播放层 {name}"
                    "（subtitle-ai-translate.md §7 分层守护）"
                )


# ---------------------------------------------------------------------------
# L2 全局校准（§5.2：已知偏移/加速可被恢复）
# ---------------------------------------------------------------------------


def _talk_pattern(n: int = 120, period_ms: int = 9000, dur_ms: int = 2200):
    """不均匀的说话模式（带抖动,避免周期性自相关歧义）。"""
    rng = np.random.default_rng(3)
    out = []
    t = 1000
    for _ in range(n):
        out.append((t, t + dur_ms + int(rng.integers(-500, 500))))
        t += period_ms + int(rng.integers(-2500, 2500))
    return out


def test_calibration_recovers_pure_offset() -> None:
    reference = _talk_pattern()
    shifted = [(s - 3000, e - 3000) for s, e in reference]  # 字幕早了 3 秒
    cal = sync.estimate_calibration(reference, shifted)
    assert cal is not None
    assert cal.scale == 1.0
    assert abs(cal.offset_ms - 3000) <= 50  # 栅格粒度内
    assert cal.score > 0.5


def test_calibration_recovers_pal_speedup() -> None:
    reference = _talk_pattern()
    a, b = 25 / 23.976, -2000
    # 参考 t' = a*s + b → 字幕 s = (t' - b)/a
    subtitle = [(int((s - b) / a), int((e - b) / a)) for s, e in reference]
    cal = sync.estimate_calibration(reference, subtitle)
    assert cal is not None
    assert abs(cal.scale - a) < 1e-6  # 假设集里选中了 25/23.976
    assert abs(cal.offset_ms - b) <= 60


def test_calibration_short_clip_recovers_negative_offset() -> None:
    """FFT 短于固定搜索窗时，负位移不能被环形索引误映射成巨大正值。"""
    reference = []
    t = 500
    for i in range(25):
        gap = 900 + (i * 137) % 800
        duration = 200 + (i * 97) % 600
        reference.append((t, t + duration))
        t += gap
    subtitle = [(s + 3000, e + 3000) for s, e in reference]

    cal = sync.estimate_calibration(reference, subtitle)

    assert cal is not None
    assert cal.scale == 1.0
    assert abs(cal.offset_ms + 3000) <= 50


def test_calibration_apply_roundtrip() -> None:
    cal = sync.Calibration(scale=1.0, offset_ms=-1500, score=1.0)
    events = [(2000, 3000, "a"), (500, 900, "b")]
    out = sync.apply_calibration(events, cal)
    assert out[0] == (500, 1500, "a")
    assert out[1] == (0, 0, "b")  # 负值截 0,end 不早于 start


def test_calibration_none_on_empty() -> None:
    assert sync.estimate_calibration([], [(0, 1000)]) is None


# ---------------------------------------------------------------------------
# G2 自动生成护栏（额度/幂等判定）
# ---------------------------------------------------------------------------


def test_auto_quota_and_skip_logic() -> None:
    from movieclaw_api.services.subtitle_gen import auto

    auto._quota_day = None
    auto._quota_used = 0
    assert auto._quota_take(2) and auto._quota_take(2)
    assert not auto._quota_take(2)  # 第三次熔断

    row = _file(
        [{"codec": "subrip", "language": "eng"}],
        [{"filename": "Movie.chs.ai.srt", "format": "srt", "language": "chi"}],
    )
    assert auto._has_target_subtitle(row, "chi")  # 已有中文（AI 产物）→ 跳过
    assert auto._has_reference(row, "chs")
    bare = _file([{"codec": "hdmv_pgs_subtitle", "language": "eng"}], [])
    assert not auto._has_target_subtitle(bare, "chi")
    assert not auto._has_reference(bare, "chs")  # 只有图形轨 → 无参考


async def test_auto_queue_claims_library_before_task_runs(monkeypatch) -> None:
    """同一轮事件循环连续触发两次扫描收尾，也只能创建一个自动批次。"""
    from movieclaw_api.services.subtitle_gen import auto

    running_libraries: set[int] = set()
    monkeypatch.setattr(auto, "_running_libraries", running_libraries)
    release = asyncio.Event()
    calls = 0

    async def fake_batch(library_id: int) -> None:
        nonlocal calls
        calls += 1
        try:
            await release.wait()
        finally:
            running_libraries.discard(library_id)

    monkeypatch.setattr(auto, "_run_batch", fake_batch)
    auto.queue_after_scan(7)
    auto.queue_after_scan(7)
    await asyncio.sleep(0)
    assert calls == 1
    assert running_libraries == {7}

    release.set()
    await asyncio.sleep(0)
    assert running_libraries == set()


def test_manual_generation_is_not_bound_to_http_background_tasks() -> None:
    """防回归：分钟级执行体不能再次挂回 Starlette 响应生命周期。"""
    from movieclaw_api.api.routes.subtitle_gen import gen_start

    assert "background_tasks" not in inspect.signature(gen_start).parameters


# ---------------------------------------------------------------------------
# 自查改进项（终检轮）：抽取缓存 / 语言 token 校验 / 校准文件名防护
# ---------------------------------------------------------------------------


def test_extract_cache_skips_when_fresh(tmp_path: Path, monkeypatch) -> None:
    """抽取产物比视频新且非空 → 直接复用（预检/发起/执行三连不重复通读容器）。"""
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    out = tmp_path / "cached.srt"
    out.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")
    import os

    os.utime(out, ns=(video.stat().st_mtime_ns + 10**9,) * 2)
    monkeypatch.setattr(extract, "ffmpeg_available", lambda: False)  # 命中缓存就不该走到这
    extract._extract_embedded_sync(video, 0, out)  # 不抛 = 缓存生效

    # 视频比产物新（洗版替换）→ 缓存失效,走抽取路径（此处 ffmpeg 缺失即抛）
    os.utime(video, ns=(out.stat().st_mtime_ns + 10**9,) * 2)
    with pytest.raises(extract.SourceLoadError):
        extract._extract_embedded_sync(video, 0, out)


def test_extract_failure_does_not_leave_cache(tmp_path: Path, monkeypatch) -> None:
    """ffmpeg 失败时只污染临时文件，最终缓存必须保持不存在。"""
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    out = tmp_path / "cached.srt"

    def failed_run(argv, **_kwargs):  # noqa: ANN001
        Path(argv[-1]).write_text("partial but non-empty", encoding="utf-8")

        class Result:
            returncode = 1
            stderr = b"decode failed"

        return Result()

    monkeypatch.setattr(extract, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(extract.subprocess, "run", failed_run)
    with pytest.raises(extract.SourceLoadError):
        extract._extract_embedded_sync(video, 0, out)
    assert not out.exists()
    assert not list(tmp_path.glob("*.part.srt"))


async def test_subtitle_job_handler_allows_different_files_to_run_concurrently(
    monkeypatch,
) -> None:
    """文件级资源锁由 Job 负责，字幕领域层不再用全局串行锁压低吞吐。"""
    from movieclaw_api.services.subtitle_gen import tasks

    running = 0
    peak = 0

    class Context:
        async def update_progress(self, **_kwargs):  # noqa: ANN003
            return None

        async def cancel_requested(self) -> bool:
            return False

    async def fake_run(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return tasks.GenResult(ok=True, message="ok")

    monkeypatch.setattr(tasks, "_run", fake_run)
    await asyncio.gather(
        tasks._run_generation_job(Context(), {"file_id": 101, "target_language": "chs"}),
        tasks._run_generation_job(Context(), {"file_id": 202, "target_language": "chs"}),
    )
    assert peak == 2


async def test_subtitle_job_handler_maps_model_configuration_error_to_blocked(
    monkeypatch,
) -> None:
    """模型配置失效是可恢复阻塞，不是普通失败或未知错误。"""
    from movieclaw_api.exceptions import NotFoundException
    from movieclaw_api.services import jobs
    from movieclaw_api.services.subtitle_gen import tasks

    class Context:
        async def update_progress(self, **_kwargs):  # noqa: ANN003
            return None

        async def cancel_requested(self) -> bool:
            return False

    async def missing_provider(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise NotFoundException("尚未配置模型供应商，请先在「设置 → AI 模型」中接入")

    monkeypatch.setattr(tasks, "_run", missing_provider)
    with pytest.raises(jobs.JobBlocked, match="尚未配置模型供应商") as captured:
        await tasks._run_generation_job(Context(), {"file_id": 101})
    assert captured.value.code == "SUBTITLE_MODEL_UNAVAILABLE"


async def test_subtitle_job_handler_leaves_unknown_error_to_dispatcher(monkeypatch) -> None:
    """未知异常交给统一 dispatcher 记录、重试和脱敏，领域层不重复吞异常。"""
    from movieclaw_api.services.subtitle_gen import tasks

    class Context:
        async def update_progress(self, **_kwargs):  # noqa: ANN003
            return None

        async def cancel_requested(self) -> bool:
            return False

    async def unexpected_failure(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("secret-upstream-payload")

    monkeypatch.setattr(tasks, "_run", unexpected_failure)
    with pytest.raises(RuntimeError, match="secret-upstream-payload"):
        await tasks._run_generation_job(Context(), {"file_id": 101})


async def test_subtitle_job_handler_cancels_pipeline_when_progress_persistence_fails(
    monkeypatch,
) -> None:
    """进度库写失败时不能遗留继续消费模型额度的孤儿翻译协程。"""
    from movieclaw_api.services.subtitle_gen import tasks

    pipeline_cancelled = asyncio.Event()

    class Context:
        async def update_progress(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("database unavailable")

        async def cancel_requested(self) -> bool:
            await asyncio.sleep(0)
            return False

    async def long_running_pipeline(*_args, **_kwargs):  # noqa: ANN002, ANN003
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pipeline_cancelled.set()
            raise

    monkeypatch.setattr(tasks, "_run", long_running_pipeline)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await tasks._run_generation_job(Context(), {"file_id": 101})
    assert pipeline_cancelled.is_set()


def test_language_token_validation() -> None:
    from movieclaw_api.exceptions import BadRequestException
    from movieclaw_api.services.subtitle_gen.tasks import ensure_language_token

    assert ensure_language_token(" CHS ") == "chs"
    for bad in ("../../etc", "a", "含中文", "x" * 20, "a/b"):
        with pytest.raises(BadRequestException):
            ensure_language_token(bad)


async def test_calibrate_rejects_path_traversal_filename() -> None:
    from movieclaw_api.exceptions import BadRequestException
    from movieclaw_api.services.subtitle_gen.tasks import calibrate_external_subtitle

    for bad in ("../x.srt", "a/b.srt", ".hidden.srt"):
        with pytest.raises(BadRequestException):
            await calibrate_external_subtitle(None, 1, bad)  # 文件名先于一切校验


async def test_compress_overruns_refine_pass() -> None:
    """超读速二次压缩（§3.3 浓缩优先闭环）：只回炉超标条,失败/变长不采纳。"""

    async def chat(system: str, user: str, _call: translate.ChatCall) -> str:
        batch = json.loads(user.rsplit("\n", 1)[1])
        out = []
        for item in batch:
            if item["i"] == 0:
                out.append({"i": 0, "t": "压缩后的短句"})
            else:
                out.append({"i": item["i"], "t": item["t"] + "反而更长了"})  # 不该被采纳
        return json.dumps(out, ensure_ascii=False)

    events = [
        (0, 1000, "这一条在一秒之内绝对读不完所以必然超标"),  # i=0 待压缩
        (2000, 3000, "这条也超标但模型给了更长的结果不能采纳啊"),  # i=1
        (4000, 10000, "正常条"),  # 不超标,不该进请求
    ]
    from movieclaw_api.services.subtitle_gen.validate import overrun_indices

    indices = overrun_indices(events)
    assert indices == [0, 1]
    out, compressed = await translate.compress_overruns(chat, events, indices, CTX, "chs")
    assert compressed == 1
    assert out[0] == (0, 1000, "压缩后的短句")
    assert out[1][2].startswith("这条也超标")  # 原译文保留
    assert out[2] == events[2]
