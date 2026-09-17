// client.go SOLO 上游客户端：llm_utils_chat / get_detail_param / ExchangeToken /
// checkin_credits / ide_user_ent_usage + 错误分类。
package upstream

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"traeapi/internal/auth"
)

// ErrKind 错误分类，pool 据此决定冷却时长（SPEC §4.3）。
type ErrKind int

const (
	ErrNone        ErrKind = iota // 成功
	ErrPlanLimit                  // 1005 + plan → 权益不足（硬冷却 12h）
	ErrSoftRate                   // 429 → 短冷却 60s
	ErrSessionDead                // 401 + Cloud-IDE-JWT 失效 → 禁用
	ErrNotFound                   // 404 → 短冷却 60s 不累计 errCount
	ErrServer                     // 5xx
	ErrClient                     // 其他 4xx
)

func (k ErrKind) String() string {
	switch k {
	case ErrPlanLimit:
		return "plan_limit"
	case ErrSoftRate:
		return "soft_rate"
	case ErrSessionDead:
		return "session_dead"
	case ErrNotFound:
		return "not_found"
	case ErrServer:
		return "server"
	case ErrClient:
		return "client"
	default:
		return "none"
	}
}

// Error 带分类的上游错误。
type Error struct {
	Kind   ErrKind
	Status int
	Msg    string
}

func (e *Error) Error() string {
	return fmt.Sprintf("upstream %s (http %d): %s", e.Kind, e.Status, e.Msg)
}

var sessionDeadMarkers = []string{"login", "token 失效", "token invalid", "session", "unauthorized", "401"}

// Classify 按 HTTP 状态码 + body 判定错误类别（SPEC §4.3）。
func Classify(status int, body string) ErrKind {
	lower := strings.ToLower(body)
	// 1005 plan 权益不足
	if strings.Contains(body, `"code":1005`) || (strings.Contains(body, "1005") && strings.Contains(lower, "plan")) {
		return ErrPlanLimit
	}
	// session 失效
	if status == http.StatusUnauthorized {
		for _, m := range sessionDeadMarkers {
			if strings.Contains(lower, strings.ToLower(m)) {
				return ErrSessionDead
			}
		}
		return ErrSessionDead
	}
	if status == http.StatusTooManyRequests {
		return ErrSoftRate
	}
	if status == http.StatusNotFound {
		return ErrNotFound
	}
	if status >= 500 {
		return ErrServer
	}
	if status >= 400 {
		return ErrClient
	}
	return ErrNone
}

// Client SOLO 上游 HTTP 客户端。Host 字段可覆盖便于测试。
type Client struct {
	// HTTP 用于短 JSON 请求（ExchangeToken/模型/签到/积分），有总超时兜底。
	HTTP *http.Client
	// StreamHTTP 用于 SSE 流式对话：不设总超时，避免长流被截断；
	// 通过 Transport.ResponseHeaderTimeout 兜底「上游一直不返回首字节」的悬挂。
	// 与 HTTP 共享同一 Transport（连接池复用）。nil 时 ChatStream 回退 HTTP。
	StreamHTTP *http.Client

	AgentHost string // https://trae-api-cn.mchost.guru
	UgHost    string // https://api.trae.cn
	OAuthHost string // https://api.trae.com.cn
	ClientID  string // en1oxy7wnw8j9n

	// CheckinRetryDelay 签到被上游风控/限流拒绝（9074）后的退避时长；
	// 0 表示用默认值（1.5s）。测试可注入更短的值以加速。
	CheckinRetryDelay time.Duration
}

// New 生产默认值。配置连接池减少 TLS 握手。
func New() *Client {
	tr := &http.Transport{
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   20,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 120 * time.Second, // 首字节兜底（长推理预留），不限制整流时长
	}
	return &Client{
		HTTP:       &http.Client{Timeout: 120 * time.Second, Transport: tr},
		StreamHTTP: &http.Client{Transport: tr}, // 无总超时
		AgentHost:  AgentHost,
		UgHost:     UgHost,
		OAuthHost:  OAuthHost,
		ClientID:   ClientID,
	}
}

func (c *Client) agentBase() string { return c.AgentHost }
func (c *Client) ugBase() string    { return c.UgHost }
func (c *Client) oauthBase() string { return c.OAuthHost }

// doJSON 发请求并解 JSON；HTTP 非 2xx 时返回带 body 片段的 *Error。
func (c *Client) doJSON(req *http.Request) (json.RawMessage, error) {
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 400 {
		kind := Classify(resp.StatusCode, string(raw))
		return nil, &Error{Kind: kind, Status: resp.StatusCode, Msg: truncate(string(raw), 200)}
	}
	return raw, nil
}

// RefreshToken 通过 ExchangeToken 强制刷新 access token（refreshToken 轮换）。
// 成功时更新 a 的字段；调用方负责 SaveAtomic。全程持 a 写锁。
func (c *Client) RefreshToken(a *auth.Auth) error {
	a.Lock()
	defer a.Unlock()
	return c.refreshLocked(a)
}

// RefreshTokenIfNeeded 仅当 token 在 skew 内即将过期（或已过期）时才刷新，
// 返回是否真正刷新。持锁内重查，避免并发请求对同一账号重复 ExchangeToken 轮换。
// 调用方仅在 returned 为 true 时需要 SaveAtomic。
func (c *Client) RefreshTokenIfNeeded(a *auth.Auth, skew time.Duration) (bool, error) {
	a.Lock()
	defer a.Unlock()
	if !a.NeedsRefreshLocked(skew) {
		return false, nil
	}
	if err := c.refreshLocked(a); err != nil {
		return false, err
	}
	return true, nil
}

// refreshLocked 是 RefreshToken 的持锁内部实现；调用方必须已持有 a 写锁。
// 任何失败路径都不改写 a 字段，保证旧 refreshToken 可重试。
func (c *Client) refreshLocked(a *auth.Auth) error {
	if strings.TrimSpace(a.RefreshToken) == "" {
		return fmt.Errorf("no refreshToken")
	}
	host := a.ApiHost
	if host == "" {
		host = c.oauthBase()
	}
	body := map[string]any{
		"ClientID":     c.ClientID,
		"RefreshToken": a.RefreshToken, // 已持 a 写锁，直接读
		"ClientSecret": "-",
		"UserID":       "",
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, host+EpExchange, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	OAuthHeaders(req)
	data, err := c.doJSON(req)
	if err != nil {
		return err
	}
	var resp struct {
		Result struct {
			Token               string `json:"Token"`
			TokenExpireAt       int64  `json:"TokenExpireAt"`
			TokenExpireDuration int64  `json:"TokenExpireDuration"`
			RefreshToken        string `json:"RefreshToken"`
			RefreshExpireAt     int64  `json:"RefreshExpireAt"`
		} `json:"Result"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return fmt.Errorf("exchange parse: %w", err)
	}
	if resp.Result.Token == "" {
		return fmt.Errorf("refresh_failed: no token in response — re-login required")
	}
	a.AccessToken = resp.Result.Token
	if resp.Result.RefreshToken != "" {
		a.RefreshToken = resp.Result.RefreshToken
	}
	// 过期时间：优先 TokenExpireAt（上游返回毫秒，需归一化为 Unix 秒）
	if resp.Result.TokenExpireAt > 0 {
		a.ExpiresAt = normalizeExpiresAt(resp.Result.TokenExpireAt)
	} else if resp.Result.TokenExpireDuration > 0 {
		a.ExpiresAt = time.Now().Add(time.Duration(resp.Result.TokenExpireDuration) * time.Second).Unix()
	}
	return nil
}

// normalizeExpiresAt 把 ExchangeToken 的 TokenExpireAt 归一化为 Unix 秒。
// 上游返回毫秒（如 1786847930141），auth 文件用秒（1786847930）。
// 毫秒时间戳 ~1.7e12，秒时间戳 ~1.7e9，用 1e12 区分。
func normalizeExpiresAt(v int64) int64 {
	if v > 1e12 {
		return v / 1000
	}
	return v
}

// ChatStream 发 llm_utils_chat 请求并返回原始 SSE body 流（调用方负责 Close）。
// 非 2xx 时 rc 为 nil、body 为上游响应体（供调用方 Classify）、err 为 nil；
// 只有传输层失败才返回 err。
func (c *Client) ChatStream(a *auth.Auth, body []byte) (rc io.ReadCloser, status int, respBody []byte, err error) {
	req, err := http.NewRequest(http.MethodPost, c.agentBase()+EpChat, bytes.NewReader(PrepareBody(body)))
	if err != nil {
		return nil, 0, nil, err
	}
	SOLOHeaders(req, a, true)
	// 用专用流客户端（无总超时），避免长 SSE 流被 HTTP.Timeout 截断。
	hc := c.HTTP
	if c.StreamHTTP != nil {
		hc = c.StreamHTTP
	}
	resp, err := hc.Do(req)
	if err != nil {
		log.Printf("chat_stream uid=%s: transport error: %v", a.UID, err)
		return nil, 0, nil, err
	}
	if resp.StatusCode >= 400 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		kind := Classify(resp.StatusCode, string(raw))
		log.Printf("chat_stream uid=%s: upstream %d %s body=%s",
			a.UID, resp.StatusCode, kind, truncate(string(raw), 200))
		return nil, resp.StatusCode, raw, nil
	}
	return resp.Body, resp.StatusCode, nil, nil
}

// ModelInfo 动态模型信息（字段来自 get_detail_param 实测结构）。
type ModelInfo struct {
	ID            string
	Name          string
	ContextWindow int64 // context_window_tokens.dev（上游真实值）
	MaxTokens     int64 // = maxOutputTokens
	// Rate 当前生效的消耗倍率：display_contact_config → consumption_rate.data.rate；
	// 若命中会员折扣则取 discount.data.consumption_rate（折后价）。
	// enable=false 或字段缺失时 HasRate=false，调用方不得臆造数值。
	Rate    float64
	HasRate bool
	// OriginalRate 折扣前倍率（无折扣时等于 Rate）。
	OriginalRate float64
	// DiscountPercent 会员折扣百分比（50 表示 5 折）；0 表示上游未提供折扣。
	DiscountPercent int
	// DiscountMatched 上游 is_discount_matched：折扣条件是否已满足
	// （未满足时仍按 OriginalRate 计费）。
	DiscountMatched bool
	// FeeLevel display_config.fee_model_level（上游费率等级，实测存在）。
	FeeLevel int
	// IsCustom display_config.is_custom_model：true 表示用户在 TRAE 客户端
	// 自行添加的模型（非官方提供），调用方通常应过滤掉。
	IsCustom bool
	// IsInvisible 上游标记 is_invisible_to_user=true：内部使用、不面向用户展示的
	// 模型（实测如 sagitta / aquila），调用方通常应过滤掉。
	IsInvisible bool
}

// FetchModels 拉 SOLO 模型表（get_detail_param）。
func (c *Client) FetchModels(a *auth.Auth) ([]ModelInfo, error) {
	body := map[string]any{
		"function":            Function,
		"config_names":        nil,
		"need_prompt":         false,
		"current_config_info": nil,
		"poly_prompt":         true,
		"mode_type":           nil,
		"agent_type":          nil,
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, c.agentBase()+EpModels, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	SOLOHeaders(req, a, false)
	data, err := c.doJSON(req)
	if err != nil {
		return nil, err
	}
	var resp struct {
		ConfigInfoList []struct {
			ConfigName string `json:"config_name"`
			// context_window_tokens 实测形如 {"dev":256000}，可能含多个键，优先取 dev。
			ContextWindowTokens map[string]int64 `json:"context_window_tokens"`
			DisplayConfig       struct {
				DisplayName   string `json:"display_name"`
				FeeModelLevel int    `json:"fee_model_level"`
				// IsCustom 用户在 TRAE 客户端自行添加的自定义模型（官方模型为 false）
				IsCustom bool `json:"is_custom_model"`
			} `json:"display_config"`
			// display_contact_config 是「JSON 字符串」，其中 consumption_rate.data.rate 为真实消耗倍率。
			DisplayContactConfig string `json:"display_contact_config"`
			// IsInvisibleToUser 上游标记「不向用户展示」的模型（实测 sagitta / aquila 为 true）。
			IsInvisibleToUser bool `json:"is_invisible_to_user"`
			ModelDetailList   []struct {
				ModelName string `json:"model_name"`
			} `json:"model_detail_list"`
		} `json:"config_info_list"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("models parse: %w", err)
	}
	out := make([]ModelInfo, 0, len(resp.ConfigInfoList))
	for _, cfg := range resp.ConfigInfoList {
		if cfg.ConfigName == "" {
			continue
		}
		name := cfg.DisplayConfig.DisplayName
		if name == "-" {
			// 上游对无展示名的模型用 "-" 占位，统一按空处理，避免面板显示 "-"
			name = ""
		}
		mi := ModelInfo{
			ID:          cfg.ConfigName,
			Name:        name,
			FeeLevel:    cfg.DisplayConfig.FeeModelLevel,
			IsCustom:    cfg.DisplayConfig.IsCustom,
			IsInvisible: cfg.IsInvisibleToUser,
		}
		if w := pickContextWindow(cfg.ContextWindowTokens); w > 0 {
			mi.ContextWindow = w
		}
		if ri := parseRateInfo(cfg.DisplayContactConfig); ri.OK {
			mi.HasRate = true
			mi.Rate = ri.Rate
			mi.OriginalRate = ri.OriginalRate
			mi.DiscountPercent = ri.DiscountPercent
			mi.DiscountMatched = ri.DiscountMatched
		}
		out = append(out, mi)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("models api returned empty list")
	}
	return out, nil
}

// pickContextWindow 从 context_window_tokens 挑上下文窗口：优先 dev，否则取最大值。
func pickContextWindow(tokens map[string]int64) int64 {
	if v, ok := tokens["dev"]; ok && v > 0 {
		return v
	}
	var max int64
	for _, v := range tokens {
		if v > max {
			max = v
		}
	}
	return max
}

// rateInfo display_contact_config 中的消耗倍率与会员折扣信息。
type rateInfo struct {
	OK              bool    // 是否成功取到倍率
	Rate            float64 // 当前生效倍率（命中折扣时为折后价）
	OriginalRate    float64 // 折扣前倍率
	DiscountPercent int     // 会员折扣百分比（50 = 5 折），0 表示无折扣
	DiscountMatched bool    // 折扣条件是否已满足
}

// parseRateInfo 解析 display_contact_config（JSON 字符串）中的真实消耗倍率与会员折扣。
//
// 实测结构：
//
//	{"consumption_rate":{"enable":true,"data":{"rate":0.78}},
//	 "discount":{"enable":true,"subKey":"member_discount",
//	             "data":{"original_consumption_rate":0.78,"consumption_rate":0.39,
//	                     "member_discount":50,"is_discount_matched":false}},
//	 "reasoning":{"enable":true}}
//
// consumption_rate.rate 是标准倍率；仅当 is_discount_matched=true 时才按
// discount.data.consumption_rate（折后价）计费。取不到时 OK=false（不臆造数值）。
func parseRateInfo(raw string) rateInfo {
	var out rateInfo
	if strings.TrimSpace(raw) == "" {
		return out
	}
	var cfg struct {
		ConsumptionRate struct {
			Enable bool `json:"enable"`
			Data   struct {
				Rate float64 `json:"rate"`
			} `json:"data"`
		} `json:"consumption_rate"`
		Discount struct {
			Enable bool `json:"enable"`
			Data   struct {
				OriginalConsumptionRate float64 `json:"original_consumption_rate"`
				ConsumptionRate         float64 `json:"consumption_rate"`
				MemberDiscount          int     `json:"member_discount"`
				IsDiscountMatched       bool    `json:"is_discount_matched"`
			} `json:"data"`
		} `json:"discount"`
	}
	if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
		return out
	}
	if !cfg.ConsumptionRate.Enable {
		return out
	}
	out.OK = true
	out.Rate = cfg.ConsumptionRate.Data.Rate
	out.OriginalRate = out.Rate

	if d := cfg.Discount; d.Enable && d.Data.MemberDiscount > 0 {
		out.DiscountPercent = d.Data.MemberDiscount
		out.DiscountMatched = d.Data.IsDiscountMatched
		if d.Data.OriginalConsumptionRate > 0 {
			out.OriginalRate = d.Data.OriginalConsumptionRate
		}
		if d.Data.IsDiscountMatched && d.Data.ConsumptionRate > 0 {
			out.Rate = d.Data.ConsumptionRate // 折扣生效，按折后价计费
		}
	}
	return out
}

// CheckinStatus 查询签到状态。
func (c *Client) CheckinStatus(a *auth.Auth) (checkedIn bool, credits int64, enable bool, err error) {
	req, err := http.NewRequest(http.MethodPost, c.ugBase()+EpCheckinStatus, bytes.NewReader([]byte("{}")))
	if err != nil {
		return false, 0, false, err
	}
	UgHeaders(req, a)
	data, err := c.doJSON(req)
	if err != nil {
		return false, 0, false, err
	}
	var resp struct {
		CheckedIn bool  `json:"checked_in"`
		Credits   int64 `json:"credits"`
		Enable    bool  `json:"enable"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return false, 0, false, fmt.Errorf("checkin status parse: %w", err)
	}
	return resp.CheckedIn, resp.Credits, resp.Enable, nil
}

// codeCheckinBusy 签到限流业务码（实测：HTTP 200 但 code=9074「当前登录用户太多，请稍后重试」）。
const codeCheckinBusy = 9074

// CheckinError 签到业务错误：HTTP 成功但响应体 code != 0。
type CheckinError struct {
	Code    int
	Message string
}

func (e *CheckinError) Error() string {
	return fmt.Sprintf("code %d: %s", e.Code, e.Message)
}

// Retryable 是否为瞬时（限流类）错误，可由调用方退避后重试。
func (e *CheckinError) Retryable() bool { return e.Code == codeCheckinBusy }

// CheckinClaim 执行签到。
//
// 上游语义（实测）：
//   - 失败同样是 HTTP 200 + body 里的 code（如 9074），所以必须解析 code；
//   - 账号当天**已签到**时，claim 幂等返回 code 0（不再校验设备号）；
//   - 当天**首次** claim 才走风控：`x-device-id` 取值决定成败 —— 实测
//     传 uid 成功，传登录流程的 32 位 GUID / 派生值 / 随机 16 位数字都被 9074 拒。
//
// 因此按 checkinDevicePlan 的候选顺序重试：首选值（uid）试两次以覆盖瞬时挤兑，
// 之后每个候选值各试一次；只有 9074 这类可重试错误才换值，其它错误立即返回。
// 全部候选都失败时返回最后一个错误（调用方照实展示，不谎报成功）。
func (c *Client) CheckinClaim(a *auth.Auth) error {
	plan := checkinDevicePlan(a)
	var lastErr error
	for i, deviceID := range plan {
		attempts := 1
		if i == 0 {
			attempts = 2 // 首选值多试一次：9074 也可能是瞬时挤兑
		}
		for k := 0; k < attempts; k++ {
			err := c.checkinClaimWith(a, deviceID)
			if err == nil {
				return nil
			}
			lastErr = err
			var ce *CheckinError
			if !errors.As(err, &ce) || !ce.Retryable() {
				return err // 非 9074：不是设备号问题，直接返回
			}
			if k < attempts-1 {
				time.Sleep(c.checkinRetryDelay())
			}
		}
		if i < len(plan)-1 {
			time.Sleep(c.checkinRetryDelay())
		}
	}
	return lastErr
}

// checkinClaimWith 用指定设备号发一次 claim。
func (c *Client) checkinClaimWith(a *auth.Auth, deviceID string) error {
	req, err := http.NewRequest(http.MethodPost, c.ugBase()+EpCheckinClaim, bytes.NewReader([]byte("{}")))
	if err != nil {
		return err
	}
	UgHeaders(req, a)
	if deviceID != "" {
		req.Header.Set("X-Device-Id", deviceID)
	}
	data, err := c.doJSON(req)
	if err != nil {
		return err
	}
	var resp struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		// 空/非 JSON body：视为成功（部分成功响应无 body）
		return nil
	}
	if resp.Code != 0 {
		return &CheckinError{Code: resp.Code, Message: resp.Message}
	}
	return nil
}

// checkinRetryDelay 返回签到重试退避时长（默认 1.5s，可由 CheckinRetryDelay 注入）。
func (c *Client) checkinRetryDelay() time.Duration {
	if c.CheckinRetryDelay > 0 {
		return c.CheckinRetryDelay
	}
	return 1500 * time.Millisecond
}

// UserEntUsage 聚合积分（ide_user_ent_usage 的 credits_limit 求和）。
func (c *Client) UserEntUsage(a *auth.Auth) (remain int64, err error) {
	remain, _, _, _, err = c.EntUsage(a)
	return remain, err
}

// EntUsage 查询账号额度明细（积分总量/已用/剩余/权益包数）。
// remain = limit - used，usage.credits_amount 是已用积分（实测）。
func (c *Client) EntUsage(a *auth.Auth) (remain, limit, used int64, packs int, err error) {
	req, err := http.NewRequest(http.MethodPost, c.ugBase()+EpEntUsage, bytes.NewReader([]byte("{}")))
	if err != nil {
		return 0, 0, 0, 0, err
	}
	UgHeaders(req, a)
	data, err := c.doJSON(req)
	if err != nil {
		return 0, 0, 0, 0, err
	}
	var resp struct {
		UserEntitlementPackList []struct {
			EntitlementBaseInfo struct {
				Quota struct {
					CreditsLimit int64 `json:"credits_limit"`
				} `json:"quota"`
			} `json:"entitlement_base_info"`
			Usage struct {
				CreditsAmount float64 `json:"credits_amount"`
			} `json:"usage"`
		} `json:"user_entitlement_pack_list"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return 0, 0, 0, 0, fmt.Errorf("ent usage parse: %w", err)
	}
	for _, p := range resp.UserEntitlementPackList {
		l := p.EntitlementBaseInfo.Quota.CreditsLimit
		if l <= 0 {
			continue
		}
		u := int64(p.Usage.CreditsAmount)
		limit += l
		used += u
		remain += l - u
		packs++
	}
	return remain, limit, used, packs, nil
}

// PoolInfo 单个权益包的额度明细。
//
// 用途：TRAE 的额度由多个包组成（免费 / 每月登录赠送 / 每日签到 / 老用户福利…），
// 计费按包顺序扣除，因此"本次扣的是哪个池"只能通过各包的 usage 变化看出来。
type PoolInfo struct {
	ID     string  `json:"id"`
	Group  string  `json:"group_name,omitempty"`
	Desc   string  `json:"desc,omitempty"`
	Limit  int64   `json:"limit"`
	Used   float64 `json:"used"`
	Remain float64 `json:"remain"`
	// NumericID 表示 entitlement_id 为纯数字。实测 Work 专属积分包的 ID
	// 是纯数字（如 358204062466），而通用包带语义前缀（free_utc… / monthly_bonus… /
	// checkin…），故以此作为「Work 专属」的判定特征（启发式）。
	NumericID bool `json:"numeric_id"`
}

// EntPools 返回账号各权益包的额度明细（按上游返回顺序）。
func (c *Client) EntPools(a *auth.Auth) ([]PoolInfo, error) {
	req, err := http.NewRequest(http.MethodPost, c.ugBase()+EpEntUsage, bytes.NewReader([]byte("{}")))
	if err != nil {
		return nil, err
	}
	UgHeaders(req, a)
	data, err := c.doJSON(req)
	if err != nil {
		return nil, err
	}
	var resp struct {
		UserEntitlementPackList []struct {
			EntitlementBaseInfo struct {
				EntitlementID string `json:"entitlement_id"`
				Quota         struct {
					CreditsLimit int64 `json:"credits_limit"`
				} `json:"quota"`
			} `json:"entitlement_base_info"`
			Usage struct {
				CreditsAmount float64 `json:"credits_amount"`
			} `json:"usage"`
			GroupName   string `json:"group_name"`
			DisplayDesc string `json:"display_desc"`
		} `json:"user_entitlement_pack_list"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("ent pools parse: %w", err)
	}
	out := make([]PoolInfo, 0, len(resp.UserEntitlementPackList))
	for _, p := range resp.UserEntitlementPackList {
		l := p.EntitlementBaseInfo.Quota.CreditsLimit
		if l <= 0 {
			continue // 免费包额度为 0，不参与计费
		}
		id := p.EntitlementBaseInfo.EntitlementID
		u := p.Usage.CreditsAmount
		out = append(out, PoolInfo{
			ID:        id,
			Group:     p.GroupName,
			Desc:      p.DisplayDesc,
			Limit:     l,
			Used:      u,
			Remain:    float64(l) - u,
			NumericID: isAllDigits(id),
		})
	}
	return out, nil
}

// isAllDigits 判断字符串是否全为数字（且非空）。
func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// GetUserInfo 查询账号信息（登录用）。
func (c *Client) GetUserInfo(a *auth.Auth) (uid, nickname, enterpriseID string, err error) {
	host := a.ApiHost
	if host == "" {
		host = c.oauthBase()
	}
	body := map[string]any{"ReqSource": "IDE", "IDEVersion": IdeVersion}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, host+EpUserInfo, bytes.NewReader(raw))
	if err != nil {
		return "", "", "", err
	}
	OAuthHeaders(req)
	req.Header.Set("X-Cloudide-Token", a.JWT()) // 读锁快照
	data, err := c.doJSON(req)
	if err != nil {
		return "", "", "", err
	}
	var resp struct {
		Result struct {
			UserID       string `json:"UserID"`
			ScreenName   string `json:"ScreenName"`
			EnterpriseID string `json:"EnterpriseID"`
		} `json:"Result"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", "", "", fmt.Errorf("userinfo parse: %w", err)
	}
	return resp.Result.UserID, resp.Result.ScreenName, resp.Result.EnterpriseID, nil
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) > n {
		return s[:n]
	}
	return s
}
