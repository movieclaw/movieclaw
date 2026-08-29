package tree

import (
	"fmt"
	"strings"
)

// useLine 是命令的用法行：命令名后跟位置参数。
func useLine(name string, pathParams []Param) string {
	parts := []string{name}
	for _, p := range pathParams {
		parts = append(parts, "<"+p.Name+">")
	}
	return strings.Join(parts, " ")
}

// shortHelp 是一行简介，危险操作带 ⚠ 前缀——让模型在选工具阶段就看见风险等级。
func shortHelp(op Operation) string {
	if op.Dangerous != "" {
		return "⚠ " + op.Summary
	}
	return op.Summary
}

// longHelp 拼出 summary + description + 示例 + 危险标注。
func longHelp(op Operation) string {
	text := op.Summary
	if op.Description != "" {
		text = op.Summary + "\n\n" + op.Description
	}
	text += "\n\n示例：\n\n  " + exampleLine(op)
	if op.Dangerous != "" {
		label := "需确认"
		if op.Dangerous == "destructive" {
			label = "破坏性操作（删除磁盘文件）"
		}
		text += fmt.Sprintf("\n\n⚠ %s：非交互执行须加 --yes", label)
	}
	return text
}

func bodyFieldUsage(f BodyField) string {
	usage := f.Description
	if f.Required {
		usage = strings.TrimSpace("[必填] " + usage)
	}
	if f.Kind == "json" {
		if usage == "" {
			usage = "JSON 字面量"
		} else {
			usage += "（JSON 字面量）"
		}
	}
	return usage
}

// exampleLine 从命令形状自动合成一条示例（spec 未提供 x-cli-examples 时的兜底）。
func exampleLine(op Operation) string {
	parts := append([]string{"mclaw"}, strings.Split(op.OperationID, ".")...)
	for _, p := range op.PathParams() {
		parts = append(parts, "<"+p.Name+">")
	}
	for _, p := range op.QueryParams() {
		if p.Required {
			parts = append(parts, fmt.Sprintf("--%s %s",
				strings.ReplaceAll(p.Name, "_", "-"), exampleValue(p.Name, p.Schema, false)))
		}
	}
	for _, f := range op.BodyFields {
		if f.Required {
			parts = append(parts, fmt.Sprintf("%s %s",
				f.FlagName(), exampleValue(f.Name, f.Schema, f.Kind == "json")))
		}
	}
	if op.IsUpload {
		parts = append(parts, "--file <本地文件>")
	}
	if op.IsDownload {
		parts = append(parts, "--output-file <保存路径>")
	}
	if op.Dangerous != "" {
		parts = append(parts, "--yes")
	}
	return strings.Join(parts, " ")
}

// semanticExamples 给常见参数配可直接理解的值，避免对人和模型都无信息量的 <值>。
var semanticExamples = map[string]string{
	"title_ref":      "tmdb:movie:438631",
	"collection_ref": "tmdb:movie:popular",
	"media_type":     "movie",
	"provider":       "tmdb",
	"kind":           "movie",
	"state":          "active",
	"root_paths":     `'["/media/movies"]'`,
	"file_ids":       `'[101,102]'`,
	"ordered_ids":    `'[1,2]'`,
}

func exampleValue(name string, schema Schema, jsonValue bool) string {
	if value, ok := semanticExamples[name]; ok {
		return value
	}
	if len(schema.Enum) > 0 {
		return schema.Enum[0]
	}
	if jsonValue {
		if schema.Type == "array" {
			return `'[1,2]'`
		}
		return `'<JSON>'`
	}
	switch schema.Type {
	case "boolean":
		return "true"
	case "integer", "number":
		return "1"
	}
	return "<" + name + ">"
}
