// Package server 暴露 OpenAI 兼容 HTTP 接口，内部驱动 pool 挑号 + upstream 转发。
package server

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"traeapi/internal/pool"
	"traeapi/internal/upstream"
)

// Config handler 依赖。
type Config struct {
	Pool         *pool.Pool
	Upstream     *upstream.Client
	APIKey       string        // 空 = 不鉴权
	AuthDir      string        // auths/ 目录，用于 import/delete 落盘 trae-*.json
	MaxRotate    int           // 单请求最多换号次数，默认 3
	PlanCooldown time.Duration // 1005 冷却，默认 12h
	SoftCooldown time.Duration // 429 冷却，默认 60s
	ErrThreshold int           // 连续错误阈值，默认 3
	ErrCooldown  time.Duration // 错误冷却，默认 10m
	RefreshSkew  time.Duration // token 预刷新窗口，默认 24h
	DefaultModel string        // 默认 glm-5.2
	// ModelRates 面板展示的模型倍率（本地参考系数，上游不提供该数据；未配置按 1.0）
	ModelRates map[string]float64
}

// maxBodyBytes 请求体大小上限（8MB），超过返回 413。
const maxBodyBytes = 8 << 20

// customModelPrefix TRAE「自定义模型」槽位的 config_name 前缀。
// 这类槽位由用户在 TRAE 客户端里接入第三方模型（Gemini / Claude / GPT-5 等），
// 上游的 is_custom_model 对它们反而是 false，因此需要按前缀一并识别。
const customModelPrefix = "custom_model_"

// isInternalModel 判断是否为 TRAE 内部功能模型（子 agent / 工具 / 会话摘要）。
// 这类模型不面向用户对话（倍率极低、多数没有独立展示名），默认从面板与 /v1/models 隐藏：
//
//	browser_use_subagent / file_search_agent / explore_sub_agent_v2 / explore_sub_agent_v13 / summary
func isInternalModel(id string) bool {
	lower := strings.ToLower(id)
	return lower == "summary" ||
		strings.HasSuffix(lower, "_agent") ||
		strings.HasSuffix(lower, "_subagent") ||
		strings.Contains(lower, "_sub_agent")
}

// Handler 主路由。
type Handler struct {
	cfg Config
	mux *http.ServeMux

	// Web 登录 pending 态：pendingID → 登录进行中的临时上下文。
	// 回调 /authorize 捕获后标记成功；面板轮询 result 取结果。
	loginMu sync.Mutex
	logins   map[string]*pendingLogin

	// stats 调用记录（内存环形缓冲，供面板「调用记录」页展示）
	stats *usageLog
}

// NewHandler 构建 handler。
func NewHandler(cfg Config) *Handler {
	if cfg.MaxRotate <= 0 {
		cfg.MaxRotate = 3
	}
	if cfg.PlanCooldown <= 0 {
		cfg.PlanCooldown = 12 * time.Hour
	}
	if cfg.SoftCooldown <= 0 {
		cfg.SoftCooldown = 60 * time.Second
	}
	if cfg.ErrThreshold <= 0 {
		cfg.ErrThreshold = 3
	}
	if cfg.ErrCooldown <= 0 {
		cfg.ErrCooldown = 10 * time.Minute
	}
	if cfg.RefreshSkew <= 0 {
		cfg.RefreshSkew = 24 * time.Hour
	}
	if cfg.DefaultModel == "" {
		cfg.DefaultModel = upstream.DefaultConfigName
	}
	h := &Handler{
		cfg:    cfg,
		mux:    http.NewServeMux(),
		logins: map[string]*pendingLogin{},
		stats:  newUsageLog(usageLogCapacity),
	}
	h.mux.HandleFunc("POST /v1/chat/completions", h.withAuth(h.chatCompletions))
	h.mux.HandleFunc("GET /v1/models", h.withAuth(h.models))
	h.mux.HandleFunc("GET /status", h.withAuth(h.status))
	h.mux.HandleFunc("GET /healthz", h.healthz)
	// 根路径重定向到管理面板，便于直接访问 http://127.0.0.1:7864 进入控制台。
	// 用 /{$} 精确匹配根路径，避免成为兜底路由吞掉其他真正需要 404 的请求。
	h.mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/admin", http.StatusFound)
	})
	// 管理面板：本地面板
	// 读接口无鉴权（局域网内只读）；写接口（accounts 写/login/refresh/authorize）
	// 经 withAdminAuth 校验 Bearer = TW2A_API_KEY（见 §4 安全设计）。
	h.mux.HandleFunc("GET /admin", h.adminPage)
	h.mux.HandleFunc("GET /admin/api/credits", h.adminCredits)
	// 一键签到（写操作，需 Bearer）：全账号并发签到 + 积分刷新 + 冷却解冻
	h.mux.HandleFunc("POST /admin/api/checkin", h.withAdminAuth(h.adminCheckin))
	// 模型列表（含本地配置倍率）与调用记录：只读
	h.mux.HandleFunc("GET /admin/api/models", h.adminModels)
	// 强制重新拉取上游模型表（写操作，需 Bearer）
	h.mux.HandleFunc("POST /admin/api/models/refresh", h.withAdminAuth(h.adminRefreshModels))
	h.mux.HandleFunc("GET /admin/api/usage", h.adminUsage)
	// 账号 CRUD
	h.mux.HandleFunc("GET /admin/api/accounts", h.adminAccounts)
	h.mux.HandleFunc("POST /admin/api/accounts/import", h.withAdminAuth(h.adminImportAccount))
	h.mux.HandleFunc("DELETE /admin/api/accounts/{uid}", h.withAdminAuth(h.adminDeleteAccount))
	h.mux.HandleFunc("PATCH /admin/api/accounts/{uid}", h.withAdminAuth(h.adminPatchAccount))
	h.mux.HandleFunc("POST /admin/api/accounts/{uid}/refresh", h.withAdminAuth(h.adminRefreshAccount))
	h.mux.HandleFunc("GET /admin/api/accounts/{uid}/json", h.adminAccountJSON)
	// Web 登录闭环
	h.mux.HandleFunc("POST /admin/api/login", h.withAdminAuth(h.adminLoginStart))
	h.mux.HandleFunc("GET /admin/api/login/result", h.adminLoginResult)
	h.mux.HandleFunc("POST /admin/api/login/cancel", h.withAdminAuth(h.adminLoginCancel))
	// TRAE 回调落点（/authorize）：无需 Bearer（TRAE 浏览器 302 不带 key），
	// 仅捕获 query 写 pending 队列，不直接落盘 token。
	h.mux.HandleFunc("GET /authorize", h.authorizeCallback)
	return h
}

// withAdminAuth 校验写操作的 Bearer API Key（常量时间比较，复用 withAuth 逻辑）。
// APIKey 为空时（未配置 TW2A_API_KEY）退化为不鉴权——本地无 key 场景仍可用，
// 但生产强烈建议配 key（见 PLAN §4）。
func (h *Handler) withAdminAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if h.cfg.APIKey == "" {
			next(w, r)
			return
		}
		authz := r.Header.Get("Authorization")
		const prefix = "Bearer "
		if len(authz) < len(prefix) || !strings.EqualFold(authz[:len(prefix)], prefix) {
			writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "missing or invalid API key")
			return
		}
		key := authz[len(prefix):]
		if subtle.ConstantTimeCompare([]byte(key), []byte(h.cfg.APIKey)) != 1 {
			writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "missing or invalid API key")
			return
		}
		next(w, r)
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.mux.ServeHTTP(w, r)
}

func (h *Handler) withAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if h.cfg.APIKey != "" {
			authz := r.Header.Get("Authorization")
			const prefix = "Bearer "
			if len(authz) < len(prefix) || !strings.EqualFold(authz[:len(prefix)], prefix) {
				writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "missing or invalid API key")
				return
			}
			key := authz[len(prefix):]
			// 常量时间比较，防时序攻击（本地代理但按规范）。
			if subtle.ConstantTimeCompare([]byte(key), []byte(h.cfg.APIKey)) != 1 {
				writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "missing or invalid API key")
				return
			}
		}
		next(w, r)
	}
}

func (h *Handler) healthz(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

func (h *Handler) status(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"accounts": h.cfg.Pool.List(),
	})
}

// ---------------------------------------------------------------------------
// 模型映射
// ---------------------------------------------------------------------------

// mapModel 将客户端传入的 model 映射为 config_name（SPEC §4.5）：
//
//	"glm-5.2"（config_name）        → 直接转发
//	"glm-5.2__dev"（内部名）        → 去掉后缀映射回 config_name
//	"auto" / ""                     → 默认模型
//	其他未知                        → 400
func (h *Handler) mapModel(model string) (string, error) {
	model = strings.TrimSpace(model)
	if model == "" || model == "auto" {
		return h.cfg.DefaultModel, nil
	}
	// 去掉内部名后缀（__dev / __max 等）
	base := model
	if i := strings.Index(model, "__"); i >= 0 {
		base = model[:i]
	}
	if h.knownModel(base) {
		return base, nil
	}
	// 宽松匹配：下划线 → 横线，大小写不敏感（deepseek_v4_pro → DeepSeek-V4-Pro）
	norm := normalizeModelName(base)
	if h.knownModel(norm) {
		return norm, nil
	}
	return "", fmt.Errorf("unknown model %q", model)
}

// normalizeModelName 将下划线命名的内部名归一化为 config_name 风格（横线分隔）。
func normalizeModelName(s string) string {
	parts := strings.Split(s, "_")
	for i, p := range parts {
		if p == "" {
			continue
		}
		parts[i] = strings.ToUpper(p[:1]) + strings.ToLower(p[1:])
	}
	return strings.Join(parts, "-")
}

// knownModel 判断 model 是否在动态/静态模型表中。
func (h *Handler) knownModel(model string) bool {
	for _, m := range h.modelList() {
		if m["id"] == model {
			return true
		}
	}
	return false
}

// 静态 SOLO 模型表（SPEC P3：32 个 config_name，来自逆向报告；动态拉取失败时回退）。
var staticModels = []map[string]any{
	{"id": "Doubao-Seed-2.1-Pro", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "seed-code-pro-0430", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "Doubao-Seed-2.1-Turbo", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "Doubao-Seed-2.0-Code", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "DeepSeek-V4-Flash-Official", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "browser_use_subagent", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "glm-5.2", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "glm-5-turbo", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "glm-5", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "DeepSeek-V4-Pro", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "DeepSeek-V4-Flash", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "kimi-k3", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "kimi-k2.7-code", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "kimi-k2.6", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "minimax-m3", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "qwen-3.7-plus", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "sagitta", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "aquila", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_gemini", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_placeholder", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_1M_text", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_1M", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_kimi", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_claude", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_gpt-5", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_no-fc", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_deepseek_chat", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_deepseek_reasoner", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "custom_model_deepseek_v4", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "explore_sub_agent_v13", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "explore_sub_agent_v2", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
	{"id": "summary", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
}

// dynamicModelsCache 动态模型缓存（成功 1h / 失败负缓存 5min）。
var dynamicModelsCache struct {
	sync.RWMutex
	ids      []upstream.ModelInfo
	fetched  time.Time
	lastFail time.Time
}

const (
	dynamicModelsTTL        = time.Hour
	modelsFetchFailCooldown = 5 * time.Minute
)

func (h *Handler) models(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"object": "list",
		"data":   h.modelList(),
	})
}

// modelList 动态获取模型列表并包装成 OpenAI 格式；失败回退静态表。
// 只返回官方模型（过滤掉用户在 TRAE 客户端自行添加的自定义模型）。
func (h *Handler) modelList() []map[string]any {
	list, _, _ := h.modelListDetailed()
	return list
}

// modelListDetailed 同 modelList，并额外返回被过滤掉的自定义模型与内部功能模型数量，
// 便于控制台提示「已隐藏 N 个自定义模型 / M 个内部模型」。
func (h *Handler) modelListDetailed() (list []map[string]any, hiddenCustom, hiddenInternal int) {
	if infos := h.fetchDynamicModels(); len(infos) > 0 {
		out := make([]map[string]any, 0, len(infos))
		for _, mi := range infos {
			// 只展示官方模型，两类都过滤：
			//   1) is_custom_model=true —— 用户在 TRAE 客户端自行添加的模型
			//   2) config_name 以 custom_model_ 开头 —— 自定义模型接入槽位
			//      （用户往槽位里接的第三方模型，官方并不提供）
			if mi.IsCustom || strings.HasPrefix(mi.ID, customModelPrefix) {
				hiddenCustom++
				continue
			}
			// 隐藏非面向用户的模型：
			//   1) 上游标记不可见「且」没有正式展示名 —— 内部代号模型（实测 sagitta / aquila
			//      的 display_name 只是占位符 "-"）。仅标记不可见但有正式名的（如旧版
			//      glm-5 / DeepSeek-V4-Pro）仍保留，因为用户可能仍在用；
			//   2) 名字符合内部功能模型模式（子 agent / 摘要）作为兜底。
			if (mi.IsInvisible && mi.Name == "") || isInternalModel(mi.ID) {
				hiddenInternal++
				continue
			}
			// 注意：mi.ContextWindow 是 int64，写入 map[string]any 后与无类型常量 0
			// 比较会因装箱成 int 而恒为 false，因此先归一化再写入。
			cw := mi.ContextWindow
			fromUpstream := cw > 0
			if !fromUpstream {
				cw = 131072 // 兜底值（非上游数据），以 context_from_upstream=false 标记
			}
			entry := map[string]any{
				"id":                    mi.ID,
				"object":                "model",
				"created":               1753600000,
				"owned_by":              "trae-solo",
				"context_length":        cw,
				"context_from_upstream": fromUpstream,
			}
			if mi.Name != "" {
				entry["name"] = mi.Name
			}
			if mi.HasRate {
				entry["rate"] = mi.Rate // 上游真实消耗倍率（命中会员折扣时为折后价）
				if mi.OriginalRate > 0 {
					entry["original_rate"] = mi.OriginalRate
				}
				if mi.DiscountPercent > 0 {
					entry["discount_percent"] = mi.DiscountPercent
					entry["discount_matched"] = mi.DiscountMatched
				}
			}
			if mi.FeeLevel > 0 {
				entry["fee_level"] = mi.FeeLevel
			}
			out = append(out, entry)
		}
		return out, hiddenCustom, hiddenInternal
	}
	// 回退静态表：同样剔除自定义模型槽位与内部功能模型
	out := make([]map[string]any, 0, len(staticModels))
	for _, m := range staticModels {
		id, _ := m["id"].(string)
		if strings.HasPrefix(id, customModelPrefix) {
			hiddenCustom++
			continue
		}
		if isInternalModel(id) {
			hiddenInternal++
			continue
		}
		out = append(out, m)
	}
	return out, hiddenCustom, hiddenInternal
}

// refreshDynamicModels 绕过缓存，立即重新拉取上游模型表并更新缓存，
// 返回上游返回的模型数量。供面板「重新拉取」按钮使用——
// 上游模型表默认有 1h 成功缓存与 5min 失败负缓存，本方法两者都跳过。
func (h *Handler) refreshDynamicModels() (int, error) {
	acct := h.cfg.Pool.Pick()
	if acct == nil {
		return 0, fmt.Errorf("no available account")
	}
	infos, err := h.cfg.Upstream.FetchModels(acct)
	if err != nil {
		dynamicModelsCache.Lock()
		dynamicModelsCache.lastFail = time.Now()
		dynamicModelsCache.Unlock()
		return 0, err
	}
	if len(infos) == 0 {
		return 0, fmt.Errorf("upstream returned empty model list")
	}
	dynamicModelsCache.Lock()
	dynamicModelsCache.ids = infos
	dynamicModelsCache.fetched = time.Now()
	dynamicModelsCache.lastFail = time.Time{}
	dynamicModelsCache.Unlock()
	return len(infos), nil
}

// fetchDynamicModels 从池中任一健康账号拉模型列表（get_detail_param），缓存 1h。
func (h *Handler) fetchDynamicModels() []upstream.ModelInfo {
	dynamicModelsCache.RLock()
	if len(dynamicModelsCache.ids) > 0 && time.Since(dynamicModelsCache.fetched) < dynamicModelsTTL {
		out := dynamicModelsCache.ids
		dynamicModelsCache.RUnlock()
		return out
	}
	if !dynamicModelsCache.lastFail.IsZero() && time.Since(dynamicModelsCache.lastFail) < modelsFetchFailCooldown {
		dynamicModelsCache.RUnlock()
		return nil
	}
	dynamicModelsCache.RUnlock()

	acct := h.cfg.Pool.Pick()
	if acct == nil {
		return nil
	}
	infos, err := h.cfg.Upstream.FetchModels(acct)
	if err != nil || len(infos) == 0 {
		dynamicModelsCache.Lock()
		dynamicModelsCache.lastFail = time.Now()
		dynamicModelsCache.Unlock()
		return nil
	}
	dynamicModelsCache.Lock()
	dynamicModelsCache.ids = infos
	dynamicModelsCache.fetched = time.Now()
	dynamicModelsCache.lastFail = time.Time{}
	dynamicModelsCache.Unlock()
	return infos
}

// ---------------------------------------------------------------------------
// chat
// ---------------------------------------------------------------------------

// setModelInBody 将 body 中 model 字段替换为 configName，并返回改写后的 body。
func setModelInBody(body []byte, configName string) []byte {
	var obj map[string]any
	if err := json.Unmarshal(body, &obj); err != nil {
		return body
	}
	obj["model"] = configName
	out, err := json.Marshal(obj)
	if err != nil {
		return body
	}
	return out
}

func (h *Handler) chatCompletions(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request", "read body: "+err.Error())
		return
	}
	if len(body) > maxBodyBytes {
		writeOpenAIError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds 8MB limit")
		return
	}
	var peek struct {
		Stream bool   `json:"stream"`
		Model  string `json:"model"`
	}
	_ = json.Unmarshal(body, &peek)

	// 调用记录：函数返回时统一落一条（含总耗时），各分支只填结果字段。
	started := time.Now()
	rec := usageRecord{Model: peek.Model, Stream: peek.Stream}
	defer func() {
		rec.DurationMS = time.Since(started).Milliseconds()
		if !rec.OK && rec.ErrCode == "" {
			rec.ErrCode = "error"
		}
		h.stats.add(rec)
	}()

	configName, err := h.mapModel(peek.Model)
	if err != nil {
		rec.ErrCode, rec.ErrMsg = "invalid_model", err.Error()
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	body = setModelInBody(body, configName)

	tried := map[string]bool{}
	var lastErr error
	for i := 0; i < h.cfg.MaxRotate; i++ {
		acct := h.cfg.Pool.PickExcluding(tried)
		if acct == nil {
			break
		}
		tried[acct.UID] = true

		// token 临近过期 → 先 refresh（持锁重查，避免并发重复轮换；失败冷却换号）
		refreshed, err := h.cfg.Upstream.RefreshTokenIfNeeded(acct, h.cfg.RefreshSkew)
		if err != nil {
			lastErr = err
			var ue *upstream.Error
			if errors.As(err, &ue) && ue.Kind == upstream.ErrSessionDead {
				h.cfg.Pool.Disable(acct.UID, "refresh session dead")
			} else {
				h.cfg.Pool.Cooldown(acct.UID, pool.CoolErr, h.cfg.ErrCooldown, "refresh: "+err.Error())
			}
			continue
		}
		if refreshed {
			_ = acct.SaveAtomic()
		}

		rc, status, respBody, terr := h.cfg.Upstream.ChatStream(acct, body)
		if terr != nil {
			lastErr = terr
			h.cfg.Pool.NoteError(acct.UID, h.cfg.ErrThreshold, h.cfg.ErrCooldown)
			continue
		}
		if status >= 400 {
			kind := upstream.Classify(status, string(respBody))
			switch kind {
			case upstream.ErrPlanLimit:
				h.cfg.Pool.Cooldown(acct.UID, pool.CoolPlan, h.cfg.PlanCooldown, "plan 权益不足")
				lastErr = &upstream.Error{Kind: kind, Status: status, Msg: string(respBody)}
				continue
			case upstream.ErrSoftRate:
				h.cfg.Pool.Cooldown(acct.UID, pool.CoolSoft, h.cfg.SoftCooldown, "429 rate limit")
				lastErr = &upstream.Error{Kind: kind, Status: status, Msg: string(respBody)}
				continue
			case upstream.ErrSessionDead:
				h.cfg.Pool.Disable(acct.UID, "session dead")
				lastErr = &upstream.Error{Kind: kind, Status: status, Msg: string(respBody)}
				continue
			case upstream.ErrNotFound:
				// 404 短冷却不累计 errCount（防雪崩）
				h.cfg.Pool.Cooldown(acct.UID, pool.CoolSoft, h.cfg.SoftCooldown, "upstream 404")
				lastErr = &upstream.Error{Kind: kind, Status: status, Msg: string(respBody)}
				continue
			default:
				h.cfg.Pool.NoteError(acct.UID, h.cfg.ErrThreshold, h.cfg.ErrCooldown)
				lastErr = &upstream.Error{Kind: kind, Status: status, Msg: string(respBody)}
				continue
			}
		}
		rec.UID, rec.Nickname = acct.UID, acct.Nickname

		if peek.Stream {
			h.cfg.Pool.NoteSuccess(acct.UID)
			rec.OK = true
			// 流内业务错误（1005 plan/5xx 等）→ 冷却账号，错误信息注入 SSE。
			// 用量经 onUsage 回调取得（流式场景上游只在 SSE 事件里给 token_usage）。
			_ = upstream.StreamWithUsage(w, rc, func(se *upstream.SOLOStreamError) {
				rec.OK = false
				rec.ErrCode = fmt.Sprintf("solo_%d", se.Code)
				rec.ErrMsg = se.Msg
				h.handleStreamError(acct.UID, se)
			}, func(u map[string]any) {
				rec.PromptTokens = usageInt(u, "prompt_tokens")
				rec.CompletionTokens = usageInt(u, "completion_tokens")
				rec.TotalTokens = usageInt(u, "total_tokens")
			})
			rc.Close()
			return
		}
		resp, err := upstream.Aggregate(rc)
		rc.Close() // 已完全消费，立即释放上游连接（防轮转 continue 泄漏 body）
		if err != nil {
			// 流内业务错误（如 1005 plan 权益不足）→ 冷却账号并轮转下一账号。
			var se *upstream.SOLOStreamError
			if errors.As(err, &se) {
				lastErr = err
				switch se.Kind() {
				case upstream.ErrPlanLimit:
					h.cfg.Pool.Cooldown(acct.UID, pool.CoolPlan, h.cfg.PlanCooldown, "plan 权益不足")
				default:
					h.cfg.Pool.NoteError(acct.UID, h.cfg.ErrThreshold, h.cfg.ErrCooldown)
				}
				continue
			}
			rec.ErrCode, rec.ErrMsg = "upstream_parse", err.Error()
			writeOpenAIError(w, http.StatusBadGateway, "upstream_parse", err.Error())
			return
		}
		h.cfg.Pool.NoteSuccess(acct.UID)
		rec.OK = true
		if u, ok := resp["usage"].(map[string]any); ok {
			rec.PromptTokens = usageInt(u, "prompt_tokens")
			rec.CompletionTokens = usageInt(u, "completion_tokens")
			rec.TotalTokens = usageInt(u, "total_tokens")
		}
		writeJSON(w, http.StatusOK, resp)
		return
	}
	msg := "all accounts unavailable (cooling/disabled)"
	if lastErr != nil {
		msg += ": " + lastErr.Error()
	}
	rec.ErrCode, rec.ErrMsg = "no_healthy_account", msg
	writeOpenAIError(w, http.StatusServiceUnavailable, "no_healthy_account", msg)
}

// handleStreamError 流式响应中的上游业务错误 → pool 冷却状态机。
// 1005 plan 权益不足 → 长冷却；其余（5xx/参数错误等）→ 累计错误冷却。
func (h *Handler) handleStreamError(uid string, se *upstream.SOLOStreamError) {
	switch se.Kind() {
	case upstream.ErrPlanLimit:
		h.cfg.Pool.Cooldown(uid, pool.CoolPlan, h.cfg.PlanCooldown, "plan 权益不足")
	default:
		h.cfg.Pool.NoteError(uid, h.cfg.ErrThreshold, h.cfg.ErrCooldown)
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	raw, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}

func writeOpenAIError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "api_error",
			"code":    code,
		},
	})
}
