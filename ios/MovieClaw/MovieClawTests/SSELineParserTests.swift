import Foundation
import Testing
@testable import MovieClaw

/// SSE 切行回归：Unicode 行分隔符不能被当成行尾（NAS「奥本海默」搜索实测复现过）
struct SSELineParserTests {
    private func parse(_ text: String) -> [ServerEvent] {
        var parser = SSELineParser()
        var out: [ServerEvent] = []
        for line in Data(text.utf8).split(separator: 0x0A, omittingEmptySubsequences: false) {
            if let event = parser.feed(Data(line)) { out.append(event) }
        }
        if let event = parser.flush() { out.append(event) }
        return out
    }

    @Test func unicodeLineSeparatorsStayInsideData() throws {
        let title = "奥本海默\u{2028}Oppenheimer\u{2029}2023\u{0085}REMUX"
        let json = #"{"title":"\#(title)"}"#
        let events = parse("event: site_result\ndata: \(json)\n\n")
        #expect(events.count == 1)
        #expect(events[0].event == "site_result")
        #expect(events[0].data == json)
    }

    @Test func crlfCommentsIdAndMultilineData() {
        let events = parse(": ping\r\nid: 7\r\nevent: job\r\ndata: a\r\ndata: b\r\n\r\ndata: tail")
        #expect(events.count == 2)
        #expect(events[0].id == "7")
        #expect(events[0].event == "job")
        #expect(events[0].data == "a\nb")
        #expect(events[1].event == "message")
        #expect(events[1].data == "tail")
    }
}
