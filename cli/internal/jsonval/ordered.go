package jsonval

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
)

// Map 是保留字段顺序的 JSON 对象。
//
// Go 的 map 无序，encoding/json 又按字典序输出，直接用 map[string]any 会把
// 服务端精心排过的字段顺序打乱：`name`、`kind` 这些人最关心的字段会被
// `auto_clear_missing` 挤到后面去。CLI 的 JSON 输出是 Agent 和脚本的稳定
// 契约，表格的列顺序更是给人扫一眼用的，两者都必须按服务端给的顺序来。
type Map struct {
	keys   []string
	values map[string]any
}

// NewMap 按给定顺序构造对象，参数是 key, value, key, value… 的交替序列。
// 用于 CLI 自己组装、字段顺序有讲究的输出（如搜索结果的行视图）。
func NewMap(pairs ...any) *Map {
	m := &Map{values: map[string]any{}}
	for i := 0; i+1 < len(pairs); i += 2 {
		key, ok := pairs[i].(string)
		if !ok {
			continue
		}
		m.Set(key, pairs[i+1])
	}
	return m
}

// Get 取字段值；不存在返回 nil。nil 接收者也安全——取值链上任一层缺失都
// 只是「没有」，不该 panic。
func (m *Map) Get(key string) any {
	if m == nil {
		return nil
	}
	return m.values[key]
}

// Set 写入字段。已存在的字段保持原位置，新字段追加到末尾。
func (m *Map) Set(key string, value any) {
	if m.values == nil {
		m.values = map[string]any{}
	}
	if _, exists := m.values[key]; !exists {
		m.keys = append(m.keys, key)
	}
	m.values[key] = value
}

// Keys 按原顺序返回字段名。
func (m *Map) Keys() []string {
	if m == nil {
		return nil
	}
	return m.keys
}

// Len 返回字段数。
func (m *Map) Len() int {
	if m == nil {
		return 0
	}
	return len(m.keys)
}

// MarshalJSON 按原顺序输出。
func (m *Map) MarshalJSON() ([]byte, error) {
	if m == nil {
		return []byte("null"), nil
	}
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, key := range m.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		encoded, err := marshalNoEscape(key)
		if err != nil {
			return nil, err
		}
		buf.Write(encoded)
		buf.WriteByte(':')
		encoded, err = marshalNoEscape(m.values[key])
		if err != nil {
			return nil, err
		}
		buf.Write(encoded)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

// UnmarshalJSON 解析对象并记录字段顺序。
func (m *Map) UnmarshalJSON(raw []byte) error {
	decoder := newDecoder(raw)
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	if delim, ok := token.(json.Delim); !ok || delim != '{' {
		return fmt.Errorf("期望 JSON 对象，实际是 %v", token)
	}
	parsed, err := decodeObject(decoder)
	if err != nil {
		return err
	}
	*m = *parsed
	return nil
}

// marshalNoEscape 序列化单个值，且不把 < > & 转成 \u00XX。
//
// encoding/json 默认为 HTML 安全做这个转义，但 CLI 的输出要喂给 jq 和人眼，
// 把种子标题里的 & 变成 & 只会碍事。
func marshalNoEscape(value any) ([]byte, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// Decode 解析一段 JSON：对象成 *Map（保序），数字成 json.Number（不丢精度，
// 也不会把 39641 打成 39641.0 或把 19 位的 ID 变成浮点近似值）。
func Decode(raw []byte) (any, error) {
	decoder := newDecoder(raw)
	value, err := decodeValue(decoder)
	if err != nil {
		return nil, err
	}
	// 确认整段读完：尾部还有内容说明这不是一个完整的 JSON 文档
	if _, err := decoder.Token(); err != io.EOF {
		return nil, fmt.Errorf("JSON 文档尾部有多余内容")
	}
	return value, nil
}

func newDecoder(raw []byte) *json.Decoder {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	return decoder
}

func decodeValue(decoder *json.Decoder) (any, error) {
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	return decodeFrom(token, decoder)
}

func decodeFrom(token json.Token, decoder *json.Decoder) (any, error) {
	delim, isDelim := token.(json.Delim)
	if !isDelim {
		return token, nil
	}
	switch delim {
	case '{':
		return decodeObject(decoder)
	case '[':
		return decodeArray(decoder)
	}
	return nil, fmt.Errorf("JSON 结构不完整：多余的 %v", delim)
}

func decodeObject(decoder *json.Decoder) (*Map, error) {
	result := &Map{values: map[string]any{}}
	for {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if delim, ok := token.(json.Delim); ok && delim == '}' {
			return result, nil
		}
		key, ok := token.(string)
		if !ok {
			return nil, fmt.Errorf("JSON 对象的键不是字符串：%v", token)
		}
		value, err := decodeValue(decoder)
		if err != nil {
			return nil, err
		}
		result.Set(key, value)
	}
}

func decodeArray(decoder *json.Decoder) ([]any, error) {
	// 非 nil 空切片：服务端返回的 [] 要原样输出成 []，不能变成 null
	items := []any{}
	for {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if delim, ok := token.(json.Delim); ok && delim == ']' {
			return items, nil
		}
		value, err := decodeFrom(token, decoder)
		if err != nil {
			return nil, err
		}
		items = append(items, value)
	}
}

// Plainify 把保序结构还原成标准库形态的 map[string]any / []any。
//
// 只有一个用途：偏斜刷新时从 /spec 拉回来的 OpenAPI 文档要交给生成层，
// 而生成层读的是 encoding/json 解出来的普通 map（内置基线 spec 走的就是
// 那条路）。业务输出一律不要用它——顺序丢了就白保了。
func Plainify(value any) any {
	switch v := value.(type) {
	case *Map:
		out := make(map[string]any, v.Len())
		for _, key := range v.Keys() {
			out[key] = Plainify(v.Get(key))
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = Plainify(item)
		}
		return out
	default:
		return value
	}
}
