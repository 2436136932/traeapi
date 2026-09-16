// constants.go SOLO 上游技术常量（SPEC §1，来自实测，禁止改动）。
package upstream

import (
	"strings"
	"sync/atomic"
)

const (
	AgentHost      = "https://trae-api-cn.mchost.guru"
	UgHost         = "https://api.trae.cn"
	OAuthHost      = "https://api.trae.com.cn"
	ConsoleHost    = "https://www.trae.cn"
	ClientID       = "en1oxy7wnw8j9n" // SOLO stable
	AppID          = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
	IdeVersion     = "0.1.52"
	IdeVersionCode = "20260811"
	DeviceBrand    = "83DG"
	OSVersion      = "Windows 11 Pro"

	// Function 默认 function（solo_work_lite）。运行时可用 SetFunction 切换。
	Function = FunctionWorkLite

	// SOLO function 取值（实测自 TraeWork 客户端 ai_agent.dll，2026-09-16）。
	// 四者都走同一个端点 /api/agent/v3/llm_utils_chat，返回的 SSE 事件序列相同。
	FunctionWorkLite     = "solo_work_lite"     // 轻量对话（本项目默认）
	FunctionWorkRemote   = "solo_work_remote"   // 远程 agent（TraeWork 原生取值）
	FunctionDesignLite   = "solo_design_lite"   // 设计模式（轻量）
	FunctionDesignRemote = "solo_design_remote" // 设计模式（远程）

	// 端点
	EpChat          = "/api/agent/v3/llm_utils_chat"
	EpModels        = "/api/ide/v1/get_detail_param"
	EpExchange      = "/cloudide/api/v3/trae/oauth/ExchangeToken"
	EpUserInfo      = "/cloudide/api/v3/trae/GetUserInfo"
	EpCheckinStatus = "/trae/api/v2/ug/checkin_credits/status"
	EpCheckinClaim  = "/trae/api/v2/ug/checkin_credits/claim"
	EpEntUsage      = "/trae/api/v2/pay/ide_user_ent_usage"
)

// knownFunctions 允许切换的 function 白名单（顺序即面板展示顺序）。
var knownFunctions = []string{FunctionWorkLite, FunctionWorkRemote, FunctionDesignLite, FunctionDesignRemote}

// activeFunction 当前使用的 function（存 string；零值 = 用默认 Function）。
var activeFunction atomic.Value

// ActiveFunction 返回当前 SOLO function；未设置时回退默认值。
func ActiveFunction() string {
	if v, ok := activeFunction.Load().(string); ok && v != "" {
		return v
	}
	return Function
}

// SetFunction 切换 SOLO function。空串表示恢复默认。
// 未知取值返回 false 且不修改当前值——避免拼错导致上游 4001（param is invalid）。
func SetFunction(name string) bool {
	name = strings.TrimSpace(name)
	if name == "" {
		activeFunction.Store("")
		return true
	}
	if !IsKnownFunction(name) {
		return false
	}
	activeFunction.Store(name)
	return true
}

// IsKnownFunction 判断是否为已知（可切换）的 function 取值。
func IsKnownFunction(name string) bool {
	for _, f := range knownFunctions {
		if f == name {
			return true
		}
	}
	return false
}

// KnownFunctions 返回可切换的 function 列表（副本，供面板展示）。
func KnownFunctions() []string {
	out := make([]string, len(knownFunctions))
	copy(out, knownFunctions)
	return out
}
