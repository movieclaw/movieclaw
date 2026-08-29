package tree

import (
	"sort"
	"strings"

	"github.com/yipengfei329/movieclaw/cli/internal/spec"
)

// simpleTypes 是能直接映射成一个标志的 JSON 类型；其余（对象、数组）收折为
// --<字段>-json。
var simpleTypes = map[string]bool{"string": true, "integer": true, "number": true, "boolean": true}

var httpMethods = []string{"get", "post", "put", "patch", "delete"}

// Schema 是解析后的 JSON Schema 片段。只保留生成命令用得上的字段。
type Schema struct {
	Type        string
	Enum        []string
	Default     any
	HasDefault  bool
	Description string
}

// Param 是 path 或 query 参数。
type Param struct {
	Name        string
	In          string // path | query
	Required    bool
	Schema      Schema
	Description string
}

// BodyField 是 requestBody 顶层拍平出来的一个字段。
type BodyField struct {
	Name        string
	Kind        string // simple | json
	Schema      Schema
	Required    bool
	Description string
}

// FlagName 是该字段对应的命令行标志。
func (f BodyField) FlagName() string {
	base := strings.ReplaceAll(f.Name, "_", "-")
	if f.Kind == "json" {
		return "--" + base + "-json"
	}
	return "--" + base
}

// DestName 是内部存取该字段值用的键（与 Python 版的 dest 同名，便于比对）。
func (f BodyField) DestName() string {
	if f.Kind == "json" {
		return f.Name + "_json"
	}
	return f.Name
}

// LongTask 是 x-cli-long-task 的声明：进度从哪读。
type LongTask struct {
	ProgressOp    string
	ProgressField string
	DoneField     string
}

// Job 是 x-cli-job 的声明：统一 Job 体系的任务 ID 在响应的哪个字段。
type Job struct {
	IDPath string
}

// Operation 是展平后的一个 spec 操作。
type Operation struct {
	OperationID string
	Method      string
	Path        string
	Summary     string
	Description string
	Params      []Param
	BodyFields  []BodyField
	HasJSONBody bool
	IsUpload    bool
	IsDownload  bool
	Hidden      bool
	// Stream 标记这是 SSE 端点；spec 里的值是对象（含 terminal_events），
	// 生成层只关心「是不是流」，终态事件名由精选层自己消费。
	Stream    bool
	Dangerous string
	LongTask  *LongTask
	Job       *Job
}

// Generable 是 P1 的生成范围：除 Web 基础设施（hidden）与 SSE 流（精选层手写
// 接入）外全部生成。
func (o Operation) Generable() bool {
	return o.OperationID != "" && !o.Hidden && !o.Stream
}

// PathParams / QueryParams 是按位置筛出的参数。
func (o Operation) PathParams() []Param  { return o.paramsIn("path") }
func (o Operation) QueryParams() []Param { return o.paramsIn("query") }

func (o Operation) paramsIn(where string) []Param {
	var out []Param
	for _, p := range o.Params {
		if p.In == where {
			out = append(out, p)
		}
	}
	return out
}

// HasInputField 判断 API 字段里是否本身就有个叫 input 的（如 session.start 的
// 用户输入）。有的话该命令放弃 --input 整体替代形态——API 字段优先。
func (o Operation) HasInputField() bool {
	for _, f := range o.BodyFields {
		if f.Name == "input" {
			return true
		}
	}
	return false
}

// IterOperations 展平 spec 中的全部操作，附带解析后的参数、body 与 x-cli 元数据。
//
// 按 (path, method) 排序输出，保证同一份 spec 每次得到同样的顺序——命令树快照
// 要逐字节稳定。
func IterOperations(doc spec.Spec) []Operation {
	components := map[string]any{}
	if comps, ok := doc["components"].(map[string]any); ok {
		if schemas, ok := comps["schemas"].(map[string]any); ok {
			components = schemas
		}
	}
	paths, _ := doc["paths"].(map[string]any)
	pathNames := make([]string, 0, len(paths))
	for name := range paths {
		pathNames = append(pathNames, name)
	}
	sort.Strings(pathNames)

	var ops []Operation
	for _, path := range pathNames {
		methods, ok := paths[path].(map[string]any)
		if !ok {
			continue
		}
		for _, method := range httpMethods {
			raw, ok := methods[method].(map[string]any)
			if !ok {
				continue
			}
			ops = append(ops, parseOperation(path, method, raw, components))
		}
	}
	return ops
}

func parseOperation(path, method string, raw, components map[string]any) Operation {
	op := Operation{
		OperationID: str(raw["operationId"]),
		Method:      method,
		Path:        path,
		Summary:     str(raw["summary"]),
		Description: str(raw["description"]),
		Hidden:      truthy(raw["x-cli-hidden"]),
		Stream:      raw["x-cli-stream"] != nil,
		Dangerous:   str(raw["x-cli-dangerous"]),
	}
	if params, ok := raw["parameters"].([]any); ok {
		for _, item := range params {
			p, ok := item.(map[string]any)
			if !ok {
				continue
			}
			where := str(p["in"])
			// 会话 Cookie 等鉴权参数由 http 层注入，不进命令
			if where != "path" && where != "query" {
				continue
			}
			schemaRaw, _ := p["schema"].(map[string]any)
			resolved := resolveSchema(schemaRaw, components)
			description := str(p["description"])
			if description == "" {
				description = resolved.Description
			}
			op.Params = append(op.Params, Param{
				Name:        str(p["name"]),
				In:          where,
				Required:    truthy(p["required"]),
				Schema:      resolved,
				Description: description,
			})
		}
	}
	content := map[string]any{}
	if body, ok := raw["requestBody"].(map[string]any); ok {
		if c, ok := body["content"].(map[string]any); ok {
			content = c
		}
	}
	if jsonBody, ok := content["application/json"].(map[string]any); ok {
		if schemaRaw, ok := jsonBody["schema"].(map[string]any); ok {
			op.HasJSONBody = true
			op.BodyFields = bodyFields(schemaRaw, components)
		}
	}
	if _, ok := content["multipart/form-data"].(map[string]any); ok {
		op.IsUpload = true
	}
	// 200 无内容 = 文件直出（FileResponse），CLI 以 --output-file 落盘
	respContent := responseContent(raw)
	op.IsDownload = method == "get" && respContent == nil

	if task, ok := raw["x-cli-long-task"].(map[string]any); ok {
		op.LongTask = &LongTask{
			ProgressOp:    str(task["progress_op"]),
			ProgressField: str(task["progress_field"]),
			DoneField:     str(task["done_field"]),
		}
	}
	if job, ok := raw["x-cli-job"].(map[string]any); ok {
		idPath := str(job["id_path"])
		if idPath == "" {
			idPath = "id"
		}
		op.Job = &Job{IDPath: idPath}
	}
	return op
}

func responseContent(raw map[string]any) map[string]any {
	responses, ok := raw["responses"].(map[string]any)
	if !ok {
		return nil
	}
	ok200, ok := responses["200"].(map[string]any)
	if !ok {
		return nil
	}
	content, _ := ok200["content"].(map[string]any)
	return content
}

// resolveSchema 展开 $ref 与 anyOf（可空类型取非 null 分支）。
func resolveSchema(schema map[string]any, components map[string]any) Schema {
	resolved := resolveRaw(schema, components, 0)
	out := Schema{
		Type:        str(resolved["type"]),
		Description: str(resolved["description"]),
	}
	if enum, ok := resolved["enum"].([]any); ok {
		for _, item := range enum {
			out.Enum = append(out.Enum, plain(item))
		}
	}
	if def, ok := resolved["default"]; ok {
		out.Default = def
		out.HasDefault = true
	}
	return out
}

func resolveRaw(schema map[string]any, components map[string]any, depth int) map[string]any {
	if schema == nil || depth > 8 {
		return map[string]any{}
	}
	if ref := str(schema["$ref"]); ref != "" {
		name := ref[strings.LastIndex(ref, "/")+1:]
		target, _ := components[name].(map[string]any)
		return resolveRaw(target, components, depth+1)
	}
	if anyOf, ok := schema["anyOf"].([]any); ok {
		for _, candidate := range anyOf {
			c, ok := candidate.(map[string]any)
			if !ok {
				continue
			}
			resolved := resolveRaw(c, components, depth+1)
			if str(resolved["type"]) != "null" {
				return resolved
			}
		}
	}
	return schema
}

// bodyFields 把 requestBody 的 JSON schema 顶层拍平为字段清单。
func bodyFields(bodySchema map[string]any, components map[string]any) []BodyField {
	schema := resolveRaw(bodySchema, components, 0)
	required := map[string]bool{}
	if req, ok := schema["required"].([]any); ok {
		for _, item := range req {
			required[plain(item)] = true
		}
	}
	props, _ := schema["properties"].(map[string]any)
	names := make([]string, 0, len(props))
	for name := range props {
		names = append(names, name)
	}
	// spec 里 properties 是对象，Go 的 map 无序；按名字排序保证命令行标志顺序稳定
	sort.Strings(names)

	fields := make([]BodyField, 0, len(names))
	for _, name := range names {
		prop, _ := props[name].(map[string]any)
		resolved := resolveSchema(prop, components)
		kind := "json"
		if simpleTypes[resolved.Type] {
			kind = "simple"
		}
		description := str(prop["description"])
		if description == "" {
			description = resolved.Description
		}
		fields = append(fields, BodyField{
			Name:        name,
			Kind:        kind,
			Schema:      resolved,
			Required:    required[name],
			Description: description,
		})
	}
	return fields
}
