package config

// systemConfigPathFn 让测试替换机器级配置路径——真实路径 /etc/movieclaw
// 在测试环境里既不可写也不该被污染。
var systemConfigPathFn = defaultSystemConfigPath

func stubSystemConfig(path string) (restore func()) {
	previous := systemConfigPathFn
	systemConfigPathFn = func() string { return path }
	return func() { systemConfigPathFn = previous }
}
