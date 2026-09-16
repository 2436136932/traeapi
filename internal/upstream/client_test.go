package upstream

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"traeapi/internal/auth"
)

func TestClassify(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   ErrKind
	}{
		{200, `{"code":1005,"message":"plan limit","extra":{"plan":2}}`, ErrPlanLimit},
		{200, `{"code":1005,"msg":"权益不足"}`, ErrPlanLimit},
		{429, ``, ErrSoftRate},
		{401, `{"code":1001,"msg":"login required"}`, ErrSessionDead},
		{401, ``, ErrSessionDead},
		{404, ``, ErrNotFound},
		{500, `boom`, ErrServer},
		{503, `unavailable`, ErrServer},
		{400, `{"code":11101,"msg":"bad param"}`, ErrClient},
		{200, `{"checked_in":false}`, ErrNone},
	}
	for _, c := range cases {
		if got := Classify(c.status, c.body); got != c.want {
			t.Errorf("Classify(%d,%q)=%v want %v", c.status, c.body, got, c.want)
		}
	}
}

type rtFunc func(*http.Request) (*http.Response, error)

func (f rtFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func jsonResp(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func testClient(fn rtFunc) *Client {
	return &Client{
		HTTP:      &http.Client{Transport: fn},
		AgentHost: "https://agent.example",
		UgHost:    "https://ug.example",
		OAuthHost: "https://oauth.example",
		ClientID:  ClientID,
	}
}

// TestCheckinClaimBusinessError 上游签到失败会返回 HTTP 200 + code!=0，
// 必须识别为错误，否则会把「当前登录用户太多」误判为签到成功（积分却不变）。
func TestCheckinClaimBusinessError(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(r.URL.Path, EpCheckinClaim) {
			return nil, errors.New("wrong path: " + r.URL.Path)
		}
		return jsonResp(200, `{"code":9074,"message":"当前登录用户太多，请稍后重试"}`), nil
	})
	err := c.CheckinClaim(&auth.Auth{UID: "u1", AccessToken: "at"})
	var ce *CheckinError
	if !errors.As(err, &ce) {
		t.Fatalf("want *CheckinError, got %v", err)
	}
	if ce.Code != 9074 || !ce.Retryable() {
		t.Errorf("code=%d retryable=%v want 9074/true", ce.Code, ce.Retryable())
	}
	if !strings.Contains(err.Error(), "9074") {
		t.Errorf("err=%v should carry code", err)
	}
	// 非限流业务码不应被判为可重试
	if (&CheckinError{Code: 9000}).Retryable() {
		t.Error("code 9000 should not be retryable")
	}
}

// TestCheckinClaimSuccess 正常成功（code=0）与空 body 都应返回 nil。
func TestCheckinClaimSuccess(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(200, `{"code":0,"message":"success"}`), nil
	})
	if err := c.CheckinClaim(&auth.Auth{UID: "u1", AccessToken: "at"}); err != nil {
		t.Fatalf("code=0: want nil, got %v", err)
	}
	c2 := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(200, ``), nil
	})
	if err := c2.CheckinClaim(&auth.Auth{UID: "u1", AccessToken: "at"}); err != nil {
		t.Fatalf("empty body: want nil, got %v", err)
	}
}

func TestRefreshTokenExchange(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(r.URL.Path, EpExchange) {
			return nil, errors.New("wrong path: " + r.URL.Path)
		}
		if r.Header.Get("Content-Type") != "application/json" {
			return nil, errors.New("missing content-type")
		}
		body, _ := io.ReadAll(r.Body)
		if !bytes.Contains(body, []byte(`"ClientID":"en1oxy7wnw8j9n"`)) || !bytes.Contains(body, []byte(`"RefreshToken":"oldrt"`)) {
			return nil, errors.New("bad body: " + string(body))
		}
		return jsonResp(200, `{"Result":{"Token":"newat","RefreshToken":"newrt","TokenExpireAt":1786805537,"TokenExpireDuration":1209600}}`), nil
	})
	a := &auth.Auth{AccessToken: "at", RefreshToken: "oldrt", ExpiresAt: 1, ApiHost: "https://oauth.example"}
	if err := c.RefreshToken(a); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if a.AccessToken != "newat" || a.RefreshToken != "newrt" {
		t.Errorf("tokens not updated: %+v", a)
	}
	if a.ExpiresAt != 1786805537 {
		t.Errorf("expiresAt=%d", a.ExpiresAt)
	}
}

// TestRefreshTokenExchangeMilliseconds 覆盖上游 TokenExpireAt 返回毫秒的场景：
// 必须归一化为 Unix 秒后再写 auth.ExpiresAt。
func TestRefreshTokenExchangeMilliseconds(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(200, `{"Result":{"Token":"newat","RefreshToken":"newrt","TokenExpireAt":1786847930141,"TokenExpireDuration":1209600}}`), nil
	})
	a := &auth.Auth{AccessToken: "at", RefreshToken: "oldrt", ExpiresAt: 1, ApiHost: "https://oauth.example"}
	if err := c.RefreshToken(a); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if a.ExpiresAt != 1786847930 {
		t.Errorf("expiresAt=%d want 1786847930 (毫秒转秒)", a.ExpiresAt)
	}
}

func TestRefreshTokenIfNeededSkipsFresh(t *testing.T) {
	calls := 0
	c := testClient(func(r *http.Request) (*http.Response, error) {
		calls++
		return jsonResp(200, `{"Result":{"Token":"newat","RefreshToken":"newrt","TokenExpireAt":1786847930141}}`), nil
	})
	a := &auth.Auth{AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999, ApiHost: "https://oauth.example"}
	refreshed, err := c.RefreshTokenIfNeeded(a, 24*3600*1e9)
	if err != nil {
		t.Fatal(err)
	}
	if refreshed {
		t.Error("fresh token should not refresh")
	}
	if calls != 0 {
		t.Errorf("ExchangeToken should not be called, calls=%d", calls)
	}
	if a.AccessToken != "at" {
		t.Error("token should remain unchanged")
	}
}

func TestRefreshTokenIfNeededRefreshesExpired(t *testing.T) {
	calls := 0
	c := testClient(func(r *http.Request) (*http.Response, error) {
		calls++
		return jsonResp(200, `{"Result":{"Token":"newat","RefreshToken":"newrt","TokenExpireAt":1786847930141}}`), nil
	})
	a := &auth.Auth{AccessToken: "at", RefreshToken: "rt", ExpiresAt: 1, ApiHost: "https://oauth.example"}
	refreshed, err := c.RefreshTokenIfNeeded(a, 24*3600*1e9)
	if err != nil {
		t.Fatal(err)
	}
	if !refreshed || calls != 1 {
		t.Errorf("expired token should refresh once, refreshed=%v calls=%d", refreshed, calls)
	}
	if a.AccessToken != "newat" || a.RefreshToken != "newrt" {
		t.Errorf("tokens not updated: %+v", a)
	}
}

func TestRefreshTokenUsesAuthApiHost(t *testing.T) {
	var gotHost string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		gotHost = r.URL.Scheme + "://" + r.URL.Host
		return jsonResp(200, `{"Result":{"Token":"newat"}}`), nil
	})
	a := &auth.Auth{AccessToken: "at", RefreshToken: "rt", ExpiresAt: 1, ApiHost: "https://custom.example"}
	if err := c.RefreshToken(a); err != nil {
		t.Fatal(err)
	}
	if gotHost != "https://custom.example" {
		t.Errorf("host=%s want auth.apiHost", gotHost)
	}
}

func TestChatStreamSendsHeadersAndRewritesBody(t *testing.T) {
	var gotAuth, gotUID, gotAppID, gotIdeVer string
	var gotBody []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		gotAuth = r.Header.Get("Authorization")
		gotUID = r.Header.Get("X-Uid")
		gotAppID = r.Header.Get("X-App-Id")
		gotIdeVer = r.Header.Get("X-Ide-Version")
		gotBody, _ = io.ReadAll(r.Body)
		return &http.Response{
			StatusCode: 200,
			Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
			Body:       io.NopCloser(strings.NewReader("event:done\ndata:{\"finish_reason\":\"stop\"}\n\n")),
		}, nil
	})
	a := &auth.Auth{AccessToken: "at", UID: "u1", MachineID: "m1", DeviceID: "d1"}
	rc, status, respBody, err := c.ChatStream(a, []byte(`{"model":"glm-5.2","messages":[]}`))
	if err != nil || status != 200 {
		t.Fatalf("chat: status=%d err=%v", status, err)
	}
	if respBody != nil {
		t.Errorf("200 response should carry nil body, got %q", respBody)
	}
	rc.Close()
	if gotAuth != "Cloud-IDE-JWT at" || gotUID != "u1" {
		t.Errorf("headers: auth=%q uid=%q", gotAuth, gotUID)
	}
	if gotAppID != AppID || gotIdeVer != IdeVersion {
		t.Errorf("app headers: appid=%q idever=%q", gotAppID, gotIdeVer)
	}
	if !bytes.Contains(gotBody, []byte(`"stream":true`)) || !bytes.Contains(gotBody, []byte(`"function":"solo_work_lite"`)) {
		t.Errorf("body not rewritten: %s", gotBody)
	}
}

func TestChatStreamUsesDedicatedStreamClient(t *testing.T) {
	// StreamHTTP 优先于 HTTP 被 ChatStream 使用（无总超时的长 SSE 流客户端）。
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: 200,
			Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
			Body:       io.NopCloser(strings.NewReader("event:done\ndata:{\"finish_reason\":\"stop\"}\n\n")),
		}, nil
	})
	c.StreamHTTP = &http.Client{Transport: c.HTTP.Transport} // 无 Timeout
	rc, status, _, err := c.ChatStream(&auth.Auth{AccessToken: "at", UID: "u1"}, []byte(`{"model":"glm-5.2","messages":[]}`))
	if err != nil || status != 200 {
		t.Fatalf("chat: status=%d err=%v", status, err)
	}
	rc.Close()
	if c.StreamHTTP.Timeout != 0 {
		t.Errorf("stream client should have no total timeout, got %v", c.StreamHTTP.Timeout)
	}
}

func TestChatStreamHTTPError(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(429, `rate limited`), nil
	})
	a := &auth.Auth{AccessToken: "at", UID: "u1"}
	_, status, respBody, err := c.ChatStream(a, []byte(`{}`))
	if status != 429 {
		t.Errorf("status=%d", status)
	}
	if err != nil {
		t.Fatalf("429 should come via status, err=%v", err)
	}
	if Classify(status, string(respBody)) != ErrSoftRate {
		t.Errorf("not classified soft rate: %q", respBody)
	}
}

func TestUserEntUsageAggregation(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(r.URL.Path, EpEntUsage) {
			return nil, errors.New("wrong path: " + r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Cloud-IDE-JWT at" {
			return nil, errors.New("missing auth header")
		}
		return jsonResp(200, `{"is_credits_billing":true,"user_entitlement_pack_list":[
			{"entitlement_base_info":{"quota":{"credits_limit":2000}}},
			{"entitlement_base_info":{"quota":{"credits_limit":500}}}
		]}`), nil
	})
	remain, err := c.UserEntUsage(&auth.Auth{AccessToken: "at"})
	if err != nil {
		t.Fatalf("ent usage: %v", err)
	}
	if remain != 2500 {
		t.Errorf("remain=%d want 2500", remain)
	}
}

func TestCheckinStatusAndClaim(t *testing.T) {
	var path string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		path = r.URL.Path
		if r.Header.Get("X-User-Region") != "CN" {
			return nil, errors.New("missing X-User-Region")
		}
		return jsonResp(200, `{"checked_in":false,"credits":200,"enable":true}`), nil
	})
	checkedIn, credits, enable, err := c.CheckinStatus(&auth.Auth{AccessToken: "at"})
	if err != nil {
		t.Fatal(err)
	}
	if checkedIn || !enable || credits != 200 {
		t.Errorf("status: checked=%v enable=%v credits=%d", checkedIn, enable, credits)
	}
	if path != EpCheckinStatus {
		t.Errorf("path=%s", path)
	}
}

// TestCheckinClaimSendsNumericDeviceID 回归测试：claim 实际发出的 X-Device-Id
// 必须是 16 位数字（32 位十六进制 GUID 会被风控固定判为 9074），
// 确保修复作用在真正调用上游的那条路径上。
func TestCheckinClaimSendsNumericDeviceID(t *testing.T) {
	var gotDeviceID string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		gotDeviceID = r.Header.Get("X-Device-Id")
		return jsonResp(200, `{"code":0,"message":"success"}`), nil
	})
	a := &auth.Auth{UID: "2666736248956905", AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	if err := c.CheckinClaim(a); err != nil {
		t.Fatal(err)
	}
	if len(gotDeviceID) != 16 || strings.Trim(gotDeviceID, "0123456789") != "" {
		t.Errorf("claim 发送的 X-Device-Id=%q want 16 位数字", gotDeviceID)
	}
}

// TestUgDeviceIDIs16DigitNumeric 上游风控要求 ug 接口的 x-device-id 是 16 位数字
// 「Aha 设备号」；登录流程落盘的是 32 位十六进制 GUID，直接使用会让签到被拒
// （HTTP 200 + code 9074「当前参与用户太多，请稍后再试」，实测可稳定复现），
// 换成 16 位数字后同一账号立即 code 0 成功。故必须派生 16 位数字设备号。
func TestUgDeviceIDIs16DigitNumeric(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", DeviceID: "07583986225ddd987138de476e6ae588"}
	got := ugDeviceID(a)
	if len(got) != 16 || strings.Trim(got, "0123456789") != "" {
		t.Fatalf("ugDeviceID=%q want 16 位数字", got)
	}
	// 稳定：同一账号反复调用结果一致（跨天、跨重启都用同一设备号）
	if again := ugDeviceID(a); again != got {
		t.Errorf("ugDeviceID 不稳定: %q vs %q", got, again)
	}
	// 唯一：不同账号设备号不同（规避「一台设备只能签一个账号」）
	b := &auth.Auth{UID: "3880644536968592", DeviceID: "a6b5c587acc77d0eae4d762ae71c7540"}
	if ugDeviceID(b) == got {
		t.Errorf("不同账号派生出相同设备号 %q", got)
	}
}

// TestUgDeviceIDKeepsNumericOverride auth 文件里显式配置 16 位数字 deviceId 时沿用，
// 便于人工为某账号指定/更换设备号（风控换绑场景）。
func TestUgDeviceIDKeepsNumericOverride(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", DeviceID: "1234567890123456"}
	if got := ugDeviceID(a); got != "1234567890123456" {
		t.Errorf("ugDeviceID=%q want 沿用显式配置值", got)
	}
}

// TestUgHeadersSendsNumericDeviceID UgHeaders 必须始终带上 16 位数字的 x-device-id，
// 否则签到会被上游风控拒绝（9074）。
func TestUgHeadersSendsNumericDeviceID(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	req, err := http.NewRequest(http.MethodPost, "https://ug.example"+EpCheckinClaim, nil)
	if err != nil {
		t.Fatal(err)
	}
	UgHeaders(req, a)
	if got := req.Header.Get("X-Device-Id"); len(got) != 16 || strings.Trim(got, "0123456789") != "" {
		t.Errorf("X-Device-Id=%q want 16 位数字", got)
	}
	if got := req.Header.Get("X-User-Region"); got != "CN" {
		t.Errorf("X-User-Region=%q", got)
	}
}
