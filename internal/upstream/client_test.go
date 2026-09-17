package upstream

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

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
		// 测试注入极短退避，避免 9074 候选重试把用例拖慢
		CheckinRetryDelay: time.Millisecond,
	}
}

// TestCheckinClaimSendsUIDDeviceID 回归测试：claim 首选把 uid 作为 x-device-id
// 发送（实测只有 uid 能通过当天首次签到的风控；32 位 GUID / 派生值 / 随机 16 位
// 数字都会被 9074 拒绝）。
func TestCheckinClaimSendsUIDDeviceID(t *testing.T) {
	var gotDeviceID, gotPath string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		gotDeviceID, gotPath = r.Header.Get("X-Device-Id"), r.URL.Path
		return jsonResp(200, `{"code":0,"message":"success"}`), nil
	})
	a := &auth.Auth{UID: "2666736248956905", AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	if err := c.CheckinClaim(a); err != nil {
		t.Fatal(err)
	}
	if gotPath != EpCheckinClaim {
		t.Errorf("path=%s", gotPath)
	}
	if gotDeviceID != a.UID {
		t.Errorf("X-Device-Id=%q want uid %q", gotDeviceID, a.UID)
	}
}

// TestCheckinClaimFallsBackToStoredDeviceID 首选值被 9074 拒后，应换候选取值重试：
// 第二个候选（账号登录时记录的设备号）返回成功即视为签到成功。
func TestCheckinClaimFallsBackToStoredDeviceID(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	var got []string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		dev := r.Header.Get("X-Device-Id")
		got = append(got, dev)
		if dev == a.UID {
			return jsonResp(200, `{"code":9074,"message":"当前参与用户太多，请稍后再试"}`), nil
		}
		return jsonResp(200, `{"code":0,"message":"success"}`), nil
	})
	if err := c.CheckinClaim(a); err != nil {
		t.Fatalf("want success on fallback, got %v", err)
	}
	// 首选 uid 会试两次（覆盖瞬时挤兑），随后换到账号设备号成功
	if len(got) != 3 {
		t.Fatalf("claim calls=%d (%v) want 3", len(got), got)
	}
	if got[0] != a.UID || got[1] != a.UID || got[2] != a.DeviceID {
		t.Errorf("device 候选顺序=%v want [uid uid stored]", got)
	}
}

// TestCheckinDevicePlanOrder 候选顺序：人工指定的 16 位数字 > uid > 账号设备号 > 派生值。
func TestCheckinDevicePlanOrder(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", DeviceID: "07583986225ddd987138de476e6ae588"}
	plan := checkinDevicePlan(a)
	if len(plan) != 3 || plan[0] != a.UID || plan[1] != a.DeviceID || !num16(plan[2]) {
		t.Errorf("plan=%v", plan)
	}
	// 人工写成 16 位数字时优先采用（换绑场景）
	b := &auth.Auth{UID: "2666736248956905", DeviceID: "1234567890123456"}
	if got := ugDeviceID(b); got != "1234567890123456" {
		t.Errorf("ugDeviceID=%q want 显式配置值", got)
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

// TestDerivedUGDeviceIDStableAndUnique 兜底派生值：稳定（同账号跨天跨重启一致）
// 且各账号互异（规避「一台设备只能签一个账号」）。
func TestDerivedUGDeviceIDStableAndUnique(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", DeviceID: "07583986225ddd987138de476e6ae588"}
	got := derivedUGDeviceID(a)
	if !num16(got) {
		t.Fatalf("derivedUGDeviceID=%q want 16 位数字", got)
	}
	if again := derivedUGDeviceID(a); again != got {
		t.Errorf("派生值不稳定: %q vs %q", got, again)
	}
	b := &auth.Auth{UID: "3880644536968592", DeviceID: "a6b5c587acc77d0eae4d762ae71c7540"}
	if derivedUGDeviceID(b) == got {
		t.Errorf("不同账号派生出相同设备号 %q", got)
	}
	// 首选值（ugDeviceID）应是 uid，而不是派生值
	if ugDeviceID(a) != a.UID {
		t.Errorf("ugDeviceID=%q want uid", ugDeviceID(a))
	}
}

// TestUgHeadersSendsDeviceID UgHeaders 必须始终带上 x-device-id（缺失会被上游
// 判为参数错误 9004），且首选 uid（唯一实测能过签到风控的取值）。
func TestUgHeadersSendsDeviceID(t *testing.T) {
	a := &auth.Auth{UID: "2666736248956905", AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	req, err := http.NewRequest(http.MethodPost, "https://ug.example"+EpCheckinClaim, nil)
	if err != nil {
		t.Fatal(err)
	}
	UgHeaders(req, a)
	if got := req.Header.Get("X-Device-Id"); got != a.UID {
		t.Errorf("X-Device-Id=%q want uid %q", got, a.UID)
	}
	if got := req.Header.Get("X-User-Region"); got != "CN" {
		t.Errorf("X-User-Region=%q", got)
	}
	// 无 uid（手建扁平 auth）时退化为 16 位数字派生值，而不是把 32 位 GUID 发出去
	b := &auth.Auth{AccessToken: "at", DeviceID: "07583986225ddd987138de476e6ae588"}
	req2, _ := http.NewRequest(http.MethodPost, "https://ug.example"+EpCheckinClaim, nil)
	UgHeaders(req2, b)
	if got := req2.Header.Get("X-Device-Id"); !num16(got) {
		t.Errorf("X-Device-Id=%q want 16 位数字派生值", got)
	}
}
