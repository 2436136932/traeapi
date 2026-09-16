package server

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"traeapi/internal/auth"
	"traeapi/internal/upstream"
)

// resetModelsCache 清空全局模型缓存，避免测试之间互相污染
// （dynamicModelsCache 是包级变量，含 1h 成功缓存与 5min 失败负缓存）。
func resetModelsCache() {
	dynamicModelsCache.Lock()
	dynamicModelsCache.ids = nil
	dynamicModelsCache.fetched = time.Time{}
	dynamicModelsCache.lastFail = time.Time{}
	dynamicModelsCache.Unlock()
}

// TestAdminModelsUpstreamRate 上游提供 context_window_tokens 与 consumption_rate 时，
// 面板应展示真实上下文窗口与真实倍率（rate_source=upstream）；
// consumption_rate.enable=false 时不得臆造倍率。
func TestAdminModelsUpstreamRate(t *testing.T) {
	resetModelsCache()
	defer resetModelsCache()

	detail := `{"config_info_list":[` +
		`{"config_name":"glm-5.2","context_window_tokens":{"dev":256000},` +
		`"display_config":{"display_name":"GLM-5.2","fee_model_level":2},` +
		`"display_contact_config":"{\"consumption_rate\":{\"enable\":true,\"data\":{\"rate\":0.78}},` +
		`\"discount\":{\"enable\":true,\"data\":{\"original_consumption_rate\":0.78,\"consumption_rate\":0.39,` +
		`\"member_discount\":50,\"is_discount_matched\":true}}}"},` +
		`{"config_name":"no-rate","context_window_tokens":{"dev":128000},` +
		`"display_config":{"display_name":"NoRate"},` +
		`"display_contact_config":"{\"consumption_rate\":{\"enable\":false,\"data\":{\"rate\":0.5}}}"},` +
		`{"config_name":"my-custom-model","context_window_tokens":{"dev":64000},` +
		`"display_config":{"display_name":"MyCustom","is_custom_model":true}},` +
		`{"config_name":"browser_use_subagent","context_window_tokens":{"dev":131000},` +
		`"display_config":{"display_name":""}},` +
		`{"config_name":"sagitta","context_window_tokens":{"dev":200000},` +
		`"display_config":{"display_name":"-"},"is_invisible_to_user":true}` +
		`]}`

	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}),
		Upstream: newRouteUpstream(map[string]string{upstream.EpModels: detail}),
	})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/admin/api/models", nil))
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		HiddenCustom   int `json:"hidden_custom"`
		HiddenInternal int `json:"hidden_internal"`
		Data           []struct {
			ID                  string  `json:"id"`
			Name                string  `json:"name"`
			ContextLength       int64   `json:"context_length"`
			ContextFromUpstream bool    `json:"context_from_upstream"`
			Rate                float64 `json:"rate"`
			RateSource          string  `json:"rate_source"`
			OriginalRate        float64 `json:"original_rate"`
			DiscountPercent     int     `json:"discount_percent"`
			DiscountMatched     bool    `json:"discount_matched"`
			FeeLevel            int     `json:"fee_level"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v body=%s", err, rec.Body)
	}
	// 第 3 个是用户自定义模型（is_custom_model=true）、第 4 个是内部功能模型，都应被过滤
	if len(resp.Data) != 2 {
		t.Fatalf("models=%d want 2 (custom + internal must be hidden): %s", len(resp.Data), rec.Body)
	}
	if resp.HiddenCustom != 1 {
		t.Errorf("hidden_custom=%d want 1", resp.HiddenCustom)
	}
	// browser_use_subagent（名字规则）与 sagitta（is_invisible_to_user=true）都应被隐藏
	if resp.HiddenInternal != 2 {
		t.Errorf("hidden_internal=%d want 2 (internal + invisible models)", resp.HiddenInternal)
	}
	first := resp.Data[0]
	if first.ID != "glm-5.2" || first.Name != "GLM-5.2" || first.FeeLevel != 2 {
		t.Errorf("display info missing: %+v", first)
	}
	// 命中会员折扣时应使用折后价（0.78 → 0.39），并带出折扣信息
	if first.Rate != 0.39 || first.RateSource != "upstream" {
		t.Errorf("matched discount should yield discounted rate: %+v", first)
	}
	if first.OriginalRate != 0.78 || first.DiscountPercent != 50 || !first.DiscountMatched {
		t.Errorf("discount info not parsed: %+v", first)
	}
	if first.ContextLength != 256000 || !first.ContextFromUpstream {
		t.Errorf("context window should come from upstream: %+v", first)
	}
	second := resp.Data[1]
	if second.Rate != 1 || second.RateSource != "default" {
		t.Errorf("disabled consumption_rate must not yield a rate: %+v", second)
	}
	if second.ContextLength != 128000 || !second.ContextFromUpstream {
		t.Errorf("second model context window wrong: %+v", second)
	}
}

// TestAdminModelsRates 动态拉取失败回退静态表时，配置的 model_rates 应生效，
// 未配置的模型标记为 default（1.0 占位）。
func TestAdminModelsRates(t *testing.T) {
	resetModelsCache()
	defer resetModelsCache()

	h := NewHandler(Config{
		Pool:       testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}),
		Upstream:   newRouteUpstream(map[string]string{}), // EpModels 404 → 回退静态表
		ModelRates: map[string]float64{"Doubao-Seed-2.1-Pro": 2.5},
	})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/admin/api/models", nil))
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		Total int `json:"total"`
		Data  []struct {
			ID            string  `json:"id"`
			ContextLength int64   `json:"context_length"`
			Rate          float64 `json:"rate"`
			RateSource    string  `json:"rate_source"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if resp.Total == 0 || len(resp.Data) == 0 {
		t.Fatal("want non-empty model list")
	}
	var found bool
	for _, m := range resp.Data {
		if m.ID == "Doubao-Seed-2.1-Pro" {
			found = true
			if m.Rate != 2.5 || m.RateSource != "config" {
				t.Errorf("configured rate not applied: %+v", m)
			}
			if m.ContextLength <= 0 {
				t.Errorf("context length missing: %+v", m)
			}
			continue
		}
		if m.Rate != 1 || m.RateSource != "default" {
			t.Errorf("unconfigured model should fall back to 1.0/default: %+v", m)
		}
	}
	if !found {
		t.Error("static model Doubao-Seed-2.1-Pro not found in /admin/api/models")
	}
}

// TestAdminUsageRecordsNonStream 非流式对话应产生一条成功记录（含 token 用量）。
func TestAdminUsageRecordsNonStream(t *testing.T) {
	up := newFakeUpstream(t, func(string) (int, string, bool) { return 200, soloSSE, true })
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999}),
		Upstream: up,
	})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`)))
	if rec.Code != 200 {
		t.Fatalf("chat code=%d body=%s", rec.Code, rec.Body)
	}

	rows := fetchUsage(t, h)
	if len(rows) != 1 {
		t.Fatalf("records=%d want 1", len(rows))
	}
	r := rows[0]
	if r.Model == "" || !r.OK || r.UID != "u1" || r.Stream {
		t.Errorf("record=%+v", r)
	}
	if r.TotalTokens != 7 { // soloSSE: prompt 5 + completion 2
		t.Errorf("total tokens=%d want 7", r.TotalTokens)
	}
}

// TestAdminUsageRecordsStreamTokens 流式对话的用量取自 SSE token_usage 事件。
func TestAdminUsageRecordsStreamTokens(t *testing.T) {
	up := newFakeUpstream(t, func(string) (int, string, bool) { return 200, soloSSE, true })
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999}),
		Upstream: up,
	})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`)))
	if rec.Code != 200 {
		t.Fatalf("stream code=%d", rec.Code)
	}

	rows := fetchUsage(t, h)
	if len(rows) != 1 {
		t.Fatalf("records=%d want 1", len(rows))
	}
	if !rows[0].Stream || !rows[0].OK || rows[0].TotalTokens != 7 {
		t.Errorf("record=%+v", rows[0])
	}
}

// TestAdminUsageRecordsFailure 无可用账号时应记录一条失败记录，便于面板排查。
func TestAdminUsageRecordsFailure(t *testing.T) {
	h := NewHandler(Config{Pool: testPoolWith(), Upstream: newRouteUpstream(map[string]string{})})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","messages":[]}`)))
	if rec.Code != 503 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}

	rows := fetchUsage(t, h)
	if len(rows) != 1 || rows[0].OK || rows[0].ErrCode != "no_healthy_account" {
		t.Errorf("records=%+v", rows)
	}
}

// TestAdminRefreshModels 强制刷新应绕过缓存重新拉上游、更新缓存，且缺 Key 返回 401。
func TestAdminRefreshModels(t *testing.T) {
	resetModelsCache()
	defer resetModelsCache()

	detail := `{"config_info_list":[{"config_name":"glm-5.2",` +
		`"context_window_tokens":{"dev":256000},` +
		`"display_config":{"display_name":"GLM-5.2"}}]}`
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}),
		Upstream: newRouteUpstream(map[string]string{upstream.EpModels: detail}),
		APIKey:   "test-key",
	})

	// 无 Key → 401（写操作需鉴权）
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/admin/api/models/refresh", strings.NewReader("{}")))
	if rec.Code != 401 {
		t.Fatalf("no key: code=%d body=%s", rec.Code, rec.Body)
	}

	// 带 Key → 200，并返回上游模型数量
	req := httptest.NewRequest("POST", "/admin/api/models/refresh", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer test-key")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		OK            bool `json:"ok"`
		UpstreamTotal int  `json:"upstream_total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if !resp.OK || resp.UpstreamTotal != 1 {
		t.Errorf("resp=%+v", resp)
	}

	// 刷新后的列表应直接反映新拉到的数据
	mrec := httptest.NewRecorder()
	h.ServeHTTP(mrec, httptest.NewRequest("GET", "/admin/api/models", nil))
	var mresp struct {
		Total int `json:"total"`
	}
	if err := json.Unmarshal(mrec.Body.Bytes(), &mresp); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if mresp.Total != 1 {
		t.Errorf("models total=%d want 1 after refresh", mresp.Total)
	}
}

// TestAdminRefreshModelsNoAccount 池中无可用账号时应失败，而不是静默返回成功。
func TestAdminRefreshModelsNoAccount(t *testing.T) {
	resetModelsCache()
	defer resetModelsCache()

	h := NewHandler(Config{
		Pool:     testPoolWith(),
		Upstream: newRouteUpstream(map[string]string{}),
		APIKey:   "test-key",
	})
	req := httptest.NewRequest("POST", "/admin/api/models/refresh", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer test-key")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code == 200 {
		t.Errorf("want failure without available account, got 200: %s", rec.Body)
	}
}

// TestAdminModelsHideInvisibleModels 开启 hide_invisible_models 后，
// 「标记不可见但有正式展示名」的旧版模型（如 glm-5）也应被隐藏，使列表贴近 TRAE 客户端。
func TestAdminModelsHideInvisibleModels(t *testing.T) {
	resetModelsCache()
	defer resetModelsCache()

	detail := `{"config_info_list":[` +
		`{"config_name":"glm-5.2","context_window_tokens":{"dev":200000},` +
		`"display_config":{"display_name":"GLM-5.2"}},` +
		`{"config_name":"glm-5","context_window_tokens":{"dev":200000},` +
		`"display_config":{"display_name":"GLM-5"},"is_invisible_to_user":true}` +
		`]}`
	up := newRouteUpstream(map[string]string{upstream.EpModels: detail})

	// 默认：不可见但有名字的模型保留（仍可调用）
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}),
		Upstream: up,
	})
	if n := modelCount(t, h); n != 2 {
		t.Errorf("default: models=%d want 2 (invisible-with-name kept)", n)
	}

	// 开启开关：只剩客户端会展示的可见模型
	resetModelsCache()
	h2 := NewHandler(Config{
		Pool:                testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}),
		Upstream:            up,
		HideInvisibleModels: true,
	})
	if n := modelCount(t, h2); n != 1 {
		t.Errorf("hide_invisible_models: models=%d want 1", n)
	}
}

// modelCount 返回 /admin/api/models 的模型总数。
func modelCount(t *testing.T, h *Handler) int {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/admin/api/models", nil))
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		Total int `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v body=%s", err, rec.Body)
	}
	return resp.Total
}

// fetchUsage 读取 /admin/api/usage 返回的记录列表。
func fetchUsage(t *testing.T, h *Handler) []usageRecord {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/admin/api/usage", nil))
	if rec.Code != 200 {
		t.Fatalf("usage code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		Records []usageRecord  `json:"records"`
		Summary map[string]any `json:"summary"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v body=%s", err, rec.Body)
	}
	if resp.Summary == nil {
		t.Error("summary missing")
	}
	return resp.Records
}
