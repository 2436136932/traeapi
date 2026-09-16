package upstream

import (
	"math"
	"net/http"
	"testing"

	"traeapi/internal/auth"
)

// TestEntPools 解析权益包明细：跳过零额度包、按 ID 特征区分「Work 专属」与通用包。
func TestEntPools(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(200, `{"user_entitlement_pack_list":[`+
			`{"entitlement_base_info":{"entitlement_id":"monthly_bonus_20269_x","quota":{"credits_limit":500}},`+
			`"usage":{"credits_amount":422.706},"group_name":"每月登录赠送"},`+
			`{"entitlement_base_info":{"entitlement_id":"358204062466","quota":{"credits_limit":2000}},`+
			`"usage":{},"group_name":"用户福利","display_desc":"老用户福利"},`+
			`{"entitlement_base_info":{"entitlement_id":"free_utc20269_x","quota":{"credits_limit":0}},`+
			`"usage":{},"group_name":"免费"}`+
			`]}`), nil
	})
	pools, err := c.EntPools(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatal(err)
	}
	if len(pools) != 2 { // 零额度的免费包不参与计费，应被过滤
		t.Fatalf("pools=%d want 2, got %+v", len(pools), pools)
	}
	common := pools[0]
	if common.ID != "monthly_bonus_20269_x" || common.Used != 422.706 {
		t.Errorf("通用包解析错误: %+v", common)
	}
	if want := 500 - 422.706; math.Abs(common.Remain-want) > 1e-9 {
		t.Errorf("通用包剩余=%v want %v", common.Remain, want)
	}
	if common.NumericID {
		t.Error("monthly_bonus 不应被判为 Work 专属")
	}
	work := pools[1]
	if work.ID != "358204062466" || work.Limit != 2000 || work.Remain != 2000 {
		t.Errorf("Work 包解析错误: %+v", work)
	}
	if !work.NumericID {
		t.Error("纯数字 ID 的包应被标记为 Work 专属特征")
	}
}

// TestIsAllDigits 边界：空串、含字母、含符号、纯数字。
func TestIsAllDigits(t *testing.T) {
	cases := map[string]bool{
		"":               false,
		"358204062466":   true,
		"monthly_bonus":  false,
		"checkin_202609": false,
		"0":              true,
	}
	for in, want := range cases {
		if got := isAllDigits(in); got != want {
			t.Errorf("isAllDigits(%q)=%v want %v", in, got, want)
		}
	}
}
