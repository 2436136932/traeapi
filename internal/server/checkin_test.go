package server

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"traeapi/internal/auth"
	"traeapi/internal/upstream"
)

// newRouteUpstream 构造按 URL path 分派固定响应的 fake 上游。
// 签到链路涉及三个不同端点（status/claim/ent_usage），需要按路径区分。
func newRouteUpstream(routes map[string]string) *upstream.Client {
	return &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			body, ok := routes[r.URL.Path]
			status := http.StatusOK
			if !ok {
				status = http.StatusNotFound
				body = `{}`
			}
			return &http.Response{
				StatusCode: status,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(body)),
			}, nil
		})},
		AgentHost: "https://fake.example",
		UgHost:    "https://fake.example",
		OAuthHost: "https://fake.example",
		ClientID:  upstream.ClientID,
		// 注入极短退避：9074 候选重试不应拖慢用例
		CheckinRetryDelay: time.Millisecond,
	}
}

// checkinAuth 返回凭证齐全、token 远未过期的测试账号（避免触发 refresh 分支）。
func checkinAuth(uid string) *auth.Auth {
	return &auth.Auth{
		UID:          uid,
		AccessToken:  "at-" + uid,
		RefreshToken: "rt-" + uid,
		ExpiresAt:    9999999999,
	}
}

func postCheckin(h *Handler, key string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("POST", "/admin/api/checkin", strings.NewReader(`{}`))
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// TestAdminCheckinClaimsAndUpdatesCredits 未签到账号应领取成功，并回写最新积分到池状态。
func TestAdminCheckinClaimsAndUpdatesCredits(t *testing.T) {
	up := newRouteUpstream(map[string]string{
		upstream.EpCheckinStatus: `{"checked_in":false,"credits":150,"enable":true}`,
		upstream.EpCheckinClaim:  `{"message":"ok"}`,
		upstream.EpEntUsage:      `{"user_entitlement_pack_list":[{"entitlement_base_info":{"quota":{"credits_limit":500}},"usage":{"credits_amount":0}}]}`,
	})
	p := testPoolWith(checkinAuth("u1"))
	h := NewHandler(Config{Pool: p, Upstream: up, APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp struct {
		Total   int `json:"total"`
		Claimed int `json:"claimed"`
		Failed  int `json:"failed"`
		Accounts []struct {
			UID     string `json:"uid"`
			Action  string `json:"action"`
			Remain  int64  `json:"remain"`
			Credits int64  `json:"checkin_credits"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v body=%s", err, rec.Body)
	}
	if resp.Total != 1 || resp.Claimed != 1 || resp.Failed != 0 {
		t.Errorf("summary=%+v", resp)
	}
	if len(resp.Accounts) != 1 || resp.Accounts[0].Action != "claimed" {
		t.Fatalf("accounts=%+v", resp.Accounts)
	}
	if resp.Accounts[0].Credits != 150 || resp.Accounts[0].Remain != 500 {
		t.Errorf("credits/remain=%+v", resp.Accounts[0])
	}
	// 签到后积分应回写池状态（ReenableIfCredits 更新 credits）
	if st, ok := p.Status("u1"); !ok || st.Credits != 500 {
		t.Errorf("pool credits not updated: %+v ok=%v", st, ok)
	}
}

// TestAdminCheckinAlreadyCheckedIn 今日已签到时不重复领取，且响应不得泄漏 token。
func TestAdminCheckinAlreadyCheckedIn(t *testing.T) {
	up := newRouteUpstream(map[string]string{
		upstream.EpCheckinStatus: `{"checked_in":true,"credits":150,"enable":true}`,
		upstream.EpEntUsage:      `{"user_entitlement_pack_list":[]}`,
	})
	h := NewHandler(Config{Pool: testPoolWith(checkinAuth("u1")), Upstream: up, APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"already":1`) || !strings.Contains(body, `"claimed":0`) {
		t.Errorf("body=%s", body)
	}
	if strings.Contains(body, "rt-u1") || strings.Contains(body, "at-u1") {
		t.Error("token leaked in checkin output")
	}
}

// TestAdminCheckinRequiresKey 写操作缺 Key 应 401。
func TestAdminCheckinRequiresKey(t *testing.T) {
	h := NewHandler(Config{
		Pool:     testPoolWith(checkinAuth("u1")),
		Upstream: newRouteUpstream(map[string]string{}),
		APIKey:   "test-key",
	})
	rec := postCheckin(h, "")
	if rec.Code != 401 {
		t.Errorf("no key: code=%d body=%s", rec.Code, rec.Body)
	}
	rec = postCheckin(h, "wrong")
	if rec.Code != 401 {
		t.Errorf("wrong key: code=%d body=%s", rec.Code, rec.Body)
	}
}

// TestAdminCheckinSkipsDisabledAccount 已禁用（session dead）账号不参与签到，计入 skipped。
func TestAdminCheckinSkipsDisabledAccount(t *testing.T) {
	p := testPoolWith(checkinAuth("u1"))
	p.Disable("u1", "session dead")
	h := NewHandler(Config{Pool: p, Upstream: newRouteUpstream(map[string]string{}), APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad json: %v body=%s", err, rec.Body)
	}
	if resp["skipped"] != float64(1) || resp["claimed"] != float64(0) {
		t.Errorf("resp=%v", resp)
	}
}

// TestAdminCheckinRetriesOnBusyCode 首次 claim 遇 9074 限流时应退避重试一次；
// 第二次成功则整次签到判定为 claimed（对应真实的「当前登录用户太多」场景）。
func TestAdminCheckinRetriesOnBusyCode(t *testing.T) {
	var mu sync.Mutex
	claimCalls := 0
	up := &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			body := `{}`
			switch r.URL.Path {
			case upstream.EpCheckinStatus:
				body = `{"checked_in":false,"credits":150,"enable":true}`
			case upstream.EpCheckinClaim:
				mu.Lock()
				claimCalls++
				n := claimCalls
				mu.Unlock()
				if n == 1 {
					body = `{"code":9074,"message":"当前登录用户太多，请稍后重试"}`
				} else {
					body = `{"code":0,"message":"success"}`
				}
			case upstream.EpEntUsage:
				body = `{"user_entitlement_pack_list":[]}`
			}
			return &http.Response{
				StatusCode: 200,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(body)),
			}, nil
		})},
		AgentHost: "https://fake.example",
		UgHost:    "https://fake.example",
		OAuthHost: "https://fake.example",
		ClientID:  upstream.ClientID,
		// 注入极短退避，保证「首次 9074 → 第二次成功」的断言语义不受影响
		CheckinRetryDelay: time.Millisecond,
	}
	h := NewHandler(Config{Pool: testPoolWith(checkinAuth("u1")), Upstream: up, APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	if !strings.Contains(rec.Body.String(), `"claimed":1`) {
		t.Errorf("want claimed after retry, body=%s", rec.Body)
	}
	mu.Lock()
	got := claimCalls
	mu.Unlock()
	if got != 2 {
		t.Errorf("claim calls=%d want 2 (initial + retry)", got)
	}
}

// TestAdminCheckinClaimFailureReported 限流持续时不得谎报成功，需如实返回失败原因。
func TestAdminCheckinClaimFailureReported(t *testing.T) {
	up := newRouteUpstream(map[string]string{
		upstream.EpCheckinStatus: `{"checked_in":false,"credits":150,"enable":true}`,
		upstream.EpCheckinClaim:  `{"code":9074,"message":"当前登录用户太多，请稍后重试"}`,
		upstream.EpEntUsage:      `{"user_entitlement_pack_list":[]}`,
	})
	h := NewHandler(Config{Pool: testPoolWith(checkinAuth("u1")), Upstream: up, APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	body := rec.Body.String()
	if !strings.Contains(body, `"failed":1`) || !strings.Contains(body, `"claimed":0`) {
		t.Errorf("body=%s", body)
	}
	if !strings.Contains(body, `"code":9074`) {
		t.Errorf("failure should carry upstream code: %s", body)
	}
}

// TestAdminCheckinEmptyPool 空账号池应正常返回零值统计，而不是报错。
func TestAdminCheckinEmptyPool(t *testing.T) {
	h := NewHandler(Config{Pool: testPoolWith(), Upstream: newRouteUpstream(map[string]string{}), APIKey: "test-key"})

	rec := postCheckin(h, "test-key")
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	var resp map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp["total"] != float64(0) {
		t.Errorf("resp=%v", resp)
	}
}
