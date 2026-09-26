import Foundation
import UIKit
import UniformTypeIdentifiers
import Testing
@testable import MovieClaw

/// AI 会话模块的纯逻辑：轨迹回放、事件归约、技能 token、Markdown 块级解析、媒体卡片参数
@MainActor
struct AgentLogicTests {
    private func decode<T: Decodable>(_ json: String, as type: T.Type = T.self) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    @Test func transcriptReplayBuildsTurnsAndInterruptedFlag() throws {
        let entries: [AgentEntry] = try decode(#"""
        [
          {"type":"message","message_id":"u1","timestamp":"2026-09-11T15:02:41+00:00","thinking_level":"high","model":null,
           "message":{"role":"user","content":[{"type":"text","text":"这个电影怎么样"},{"type":"image","attachment_id":"a1","name":"图.jpg"}]}},
          {"type":"message","message_id":"m2","timestamp":"2026-09-11T15:02:46+00:00",
           "message":{"role":"assistant","content":[{"type":"thinking","text":"想一想"}],"tool_calls":[{"id":"c1","name":"bash","arguments":{"command":"ls  -la"}}]}},
          {"type":"message","message_id":"m3","timestamp":"2026-09-11T15:02:47+00:00","message":{"role":"tool","tool_call_id":"c1","content":"ok"}},
          {"type":"message","message_id":"m4","timestamp":"2026-09-11T15:02:50+00:00","message":{"role":"assistant","content":"答案"}},
          {"type":"compaction","compaction_id":"k","timestamp":"2026-09-11T15:03:00+00:00","summary":"摘要","tokens_before":9000,"tokens_after":800,"replacement_history":[]},
          {"type":"message","message_id":"u2","timestamp":"2026-09-11T15:05:00+00:00","message":{"role":"user","content":"再来"}},
          {"type":"message","message_id":"m5","timestamp":"2026-09-11T15:05:02+00:00","message":{"role":"assistant","content":[],"tool_calls":[{"id":"c2","name":"mclaw","arguments":{"args":"status"}}]}}
        ]
        """#)
        let turns = AgentTimeline.turns(from: entries)
        #expect(turns.count == 2)
        #expect(turns[0].images == [AgentTurnImage(attachmentId: "a1", name: "图.jpg")])
        #expect(turns[0].thinkingLevel == .some("high"))
        #expect(turns[0].interrupted == false)
        #expect(turns[0].answerText == "答案")
        guard case let .process(items) = turns[0].segments.first, case let .tool(tool) = items.last else {
            Issue.record("首段应是处理过程块")
            return
        }
        #expect(tool.output == "ok")
        #expect(tool.summary == "ls -la")
        #expect(AgentTimeline.processSummary(items) == "已思考，执行 1 次命令")
        if case .compaction = turns[0].segments.last {} else { Issue.record("压缩卡片应并入上一轮") }
        // 第二轮没有以无工具调用的正文收尾：已中断
        #expect(turns[1].interrupted)
    }

    @Test func streamEventsReduceIntoTimeline() throws {
        var turn = AgentTurn(id: "t", input: "q", status: .running, startedAt: .now)
        let events: [AgentStreamEvent] = try decode(#"""
        [
          {"type":"thinking_delta","delta":"想"},
          {"type":"tool_call_start","tool_call":{"id":"c1","name":"mclaw"}},
          {"type":"tool_call_delta","tool_call_id":"c1","delta":"{\"args\":"},
          {"type":"tool_call","tool_call":{"id":"c1","name":"mclaw","arguments":{"args":"library list"}}},
          {"type":"tool_result","tool_result":{"tool_call_id":"c1","name":"mclaw","output":"[]","is_error":false,"elapsed_ms":3}},
          {"type":"text_delta","delta":"你好"},
          {"type":"text_delta","delta":"世界"},
          {"type":"agent_done","result":{"elapsed_ms":1234,"steps":2,"usage":{"prompt_tokens":1,"completion_tokens":2}}}
        ]
        """#)
        for event in events { AgentTimeline.apply(event, to: &turn) }
        #expect(turn.status == .done)
        #expect(turn.answerText == "你好世界")
        #expect(turn.result?.elapsedMs == 1234)
        guard case let .process(items) = turn.segments.first, case let .tool(tool) = items.last else {
            Issue.record("首段应是处理过程块")
            return
        }
        #expect(tool.label == #"mclaw({"args":"library list"})"#)
        #expect(tool.argsDone == true)
        #expect(tool.output == "[]")
        var cancelled = AgentTurn(id: "x", input: "q", status: .running, startedAt: .now)
        AgentTimeline.apply(try decode(#"{"type":"agent_cancelled"}"#), to: &cancelled)
        #expect(cancelled.stopped && cancelled.status == .done)
    }

    @Test func skillTokensRoundTrip() {
        let expanded = "<skill name=\"diagnose\" location=\"/x/SKILL.md\">\n正文\n</skill>\n\n帮我看看"
        #expect(AgentSkillText.toTokenForm(expanded) == "/skill:diagnose 帮我看看")
        let parsed = AgentSkillText.parseTokens("/skill:diagnose /skill:typo 帮我看看", allow: ["diagnose"])
        #expect(parsed.names == ["diagnose"])
        #expect(parsed.text == "/skill:typo 帮我看看")
        #expect(AgentSkillText.slashQuery(in: "你好 /dia")?.query == "dia")
        #expect(AgentSkillText.slashQuery(in: "a/b") == nil)
        var draft = AgentDraft()
        draft.load("/skill:diagnose 帮我看看")
        #expect(draft.skills == ["diagnose"] && draft.text == "帮我看看")
        #expect(draft.message == "/skill:diagnose 帮我看看")
    }

    @Test func markdownBlocks() {
        let blocks = AgentMarkdownParser.parse("""
        ## 标题
        第一行
        第二行

        - 项一
          - 嵌套
        - [x] 完成

        | 剧集 | 评分 |
        |---|---|
        | 猎犬 | 8.5 |

        ```bash
        ls -la
        ```
        > 引用
        ---
        """)
        #expect(blocks.count == 7)
        #expect(blocks[0] == .heading(level: 2, text: "标题"))
        #expect(blocks[1] == .paragraph("第一行 第二行"))
        if case let .list(ordered, _, items) = blocks[2] {
            #expect(!ordered && items.count == 2)
            #expect(items[1].checked == true)
            if case .list = items[0].blocks.last {} else { Issue.record("嵌套列表应归入第一项") }
        } else { Issue.record("应解析出列表") }
        #expect(blocks[3] == .table(header: ["剧集", "评分"], rows: [["猎犬", "8.5"]]))
        #expect(blocks[4] == .code(language: "bash", text: "ls -la"))
        #expect(blocks[5] == .quote([.paragraph("引用")]))
        #expect(blocks[6] == .rule)
    }

    @Test func markdownImagesBecomeBlocks() {
        let blocks = AgentMarkdownParser.parse("""
        海报如下：![沙丘](https://image.tmdb.org/t/p/w500/a.jpg "标题") 请查看

        ![](/images/assets/1.jpg)
        """)
        #expect(blocks == [
            .paragraph("海报如下："),
            .image(alt: "沙丘", url: "https://image.tmdb.org/t/p/w500/a.jpg"),
            .paragraph("请查看"),
            .image(alt: "", url: "/images/assets/1.jpg"),
        ])
        #expect(AgentMarkdownParser.parse("普通 [链接](https://a.b) 文本") == [.paragraph("普通 [链接](https://a.b) 文本")])
    }

    @Test func attachmentNamesFollowWeb() throws {
        // 压缩/转码后换 .jpg，没有原名兜底「图片」（同 Web compressImage）
        #expect(AgentImageCompressor.jpegName("IMG_0001.HEIC") == "IMG_0001.jpg")
        #expect(AgentImageCompressor.jpegName("截图.png") == "截图.jpg")
        #expect(AgentImageCompressor.jpegName(nil) == "图片.jpg")
        // 小图原样上传保留原名
        let png = try #require(UIGraphicsImageRenderer(size: CGSize(width: 8, height: 8)).image { _ in }.pngData())
        #expect(try AgentImageCompressor.prepare(png, contentType: .png, filename: "a.png").filename == "a.png")
        #expect(try AgentImageCompressor.prepare(png, contentType: .png).filename == "图片.png")
        #expect(try AgentImageCompressor.prepare(png, contentType: .heic, filename: "IMG_1.HEIC").filename == "IMG_1.jpg")
    }

    @Test func mediaCardArgs() throws {
        let args: AgentJSONObject = try decode(#"{"component":"title","title":"你说的这部","items":[{"title_ref":"tmdb:tv:232766"},{"tmdb_id":5,"media_type":"movie"},{"title_ref":"bad"},{"title_ref":"tmdb:tv:232766"}]}"#)
        let group = try #require(AgentMediaCards.parse(name: "show_media_cards_v1", args: args))
        #expect(group.title == "你说的这部")
        #expect(group.cards == [.title(titleRef: "tmdb:tv:232766"), .title(titleRef: "tmdb:movie:5")])
        #expect(AgentMediaCards.parse(name: "show_media_cards_v2", args: args) == nil)
    }
}
