package upstream

import (
	"strings"
	"testing"
)

// TestSetFunctionSwitch 验证 function 热切换：默认值、合法切换、非法值拒绝、恢复默认。
func TestSetFunctionSwitch(t *testing.T) {
	defer SetFunction("") // 每个用例结束后恢复默认，避免影响其它测试（PrepareBody 依赖全局值）

	if got := ActiveFunction(); got != FunctionWorkLite {
		t.Fatalf("默认 function=%q want %q", got, FunctionWorkLite)
	}
	if !SetFunction(FunctionWorkRemote) {
		t.Fatal("切换到 solo_work_remote 应成功")
	}
	if got := ActiveFunction(); got != FunctionWorkRemote {
		t.Errorf("切换后 function=%q want %q", got, FunctionWorkRemote)
	}
	// 未知取值必须被拒绝，否则会打到上游报 4001（param is invalid）
	if SetFunction("solo_work_bogus") {
		t.Error("未知 function 应被拒绝")
	}
	if got := ActiveFunction(); got != FunctionWorkRemote {
		t.Errorf("被拒绝的取值不应改变当前值，function=%q", got)
	}
	// 空串 = 恢复默认
	if !SetFunction("") {
		t.Fatal("恢复默认应成功")
	}
	if got := ActiveFunction(); got != FunctionWorkLite {
		t.Errorf("恢复默认后 function=%q want %q", got, FunctionWorkLite)
	}
	// 允许的值都必须能切（白名单与 ActiveFunction 一致）
	for _, f := range KnownFunctions() {
		if !SetFunction(f) || ActiveFunction() != f {
			t.Errorf("白名单取值 %q 无法切换", f)
		}
	}
}

// TestPrepareBodyUsesActiveFunction 请求体必须反映当前切换的 function。
func TestPrepareBodyUsesActiveFunction(t *testing.T) {
	defer SetFunction("")

	SetFunction(FunctionWorkRemote)
	out := string(PrepareBody([]byte(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`)))
	if !strings.Contains(out, `"function":"solo_work_remote"`) {
		t.Errorf("body 未使用当前 function: %s", out)
	}

	SetFunction("")
	out = string(PrepareBody([]byte(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`)))
	if !strings.Contains(out, `"function":"solo_work_lite"`) {
		t.Errorf("恢复默认后 body 未使用 solo_work_lite: %s", out)
	}
}

// TestKnownFunctionsCoversTraeWorkValues 白名单必须包含 TraeWork 客户端实测的取值。
func TestKnownFunctionsCoversTraeWorkValues(t *testing.T) {
	for _, want := range []string{"solo_work_lite", "solo_work_remote", "solo_design_lite", "solo_design_remote"} {
		if !IsKnownFunction(want) {
			t.Errorf("白名单缺少 %q", want)
		}
	}
}
