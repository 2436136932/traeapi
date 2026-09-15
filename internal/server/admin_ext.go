// admin_ext.go /admin/api/models 与 /admin/api/usage：面板「模型」「调用记录」数据源（只读）。
package server

import (
	"net/http"
	"strconv"
	"time"
)

// adminRefreshModels POST /admin/api/models/refresh：绕过缓存立即重新拉取上游模型表。
// 写操作（会触发一次上游请求），经 withAdminAuth 校验 Bearer。
func (h *Handler) adminRefreshModels(w http.ResponseWriter, r *http.Request) {
	n, err := h.refreshDynamicModels()
	if err != nil {
		writeOpenAIError(w, http.StatusBadGateway, "refresh_models_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":             true,
		"upstream_total": n,
		"refreshed_at":   time.Now().Format("2006-01-02 15:04:05"),
	})
}

// modelEntry 面板展示的模型条目。
type modelEntry struct {
	ID            string `json:"id"`
	Name          string `json:"name,omitempty"`
	ContextLength int64  `json:"context_length"`
	// ContextFromUpstream 标识 context_length 是否取自上游真实字段
	// （上游缺该字段时兜底 131072，此时为 false）。
	ContextFromUpstream bool `json:"context_from_upstream"`
	Rate                float64 `json:"rate"`
	// RateSource 倍率来源：
	//   upstream —— 上游 display_contact_config → consumption_rate.data.rate（真实值）
	//   config   —— config.json 的 model_rates（用户自配）
	//   default  —— 都没有时的 1.0 占位（非真实数据）
	RateSource string `json:"rate_source"`
	FeeLevel   int    `json:"fee_level,omitempty"`
	// OriginalRate 折扣前倍率；DiscountPercent 会员折扣百分比（50 = 5 折）；
	// DiscountMatched 标识该折扣当前是否已生效（未生效时按 OriginalRate 计费）。
	OriginalRate    float64 `json:"original_rate,omitempty"`
	DiscountPercent int     `json:"discount_percent,omitempty"`
	DiscountMatched bool    `json:"discount_matched,omitempty"`
}

// adminModels GET /admin/api/models：列出可用模型与倍率（只读，无鉴权）。
//
// 说明：上游 get_detail_param 并不返回模型倍率，这里的倍率是本地参考系数，
// 未配置的模型一律按 1.0 展示，用户可按实际消耗自行在 config.json 调整。
func (h *Handler) adminModels(w http.ResponseWriter, r *http.Request) {
	list, hiddenCustom, hiddenInternal := h.modelListDetailed() // 复用 /v1/models 的来源（动态获取 + 静态表回退）
	out := make([]modelEntry, 0, len(list))
	for _, m := range list {
		id := asString(m["id"])
		if id == "" {
			continue
		}
		e := modelEntry{
			ID:            id,
			Name:          asString(m["name"]),
			ContextLength: asInt64(m["context_length"]),
			Rate:          1,
			RateSource:    "default",
			FeeLevel:      int(asInt64(m["fee_level"])),
		}
		if b, ok := m["context_from_upstream"].(bool); ok {
			e.ContextFromUpstream = b
		}
		// 倍率优先级：上游真实值 > config.json 配置 > 默认 1.0 占位
		if v, ok := m["rate"].(float64); ok && v > 0 {
			e.Rate, e.RateSource = v, "upstream"
			if ov, ok := m["original_rate"].(float64); ok {
				e.OriginalRate = ov
			}
			// 注意：modelList 里的 discount_percent 是 int，这里必须用宽松转换，
			// 直接断言 float64 会失败导致折扣百分比丢失。
			e.DiscountPercent = int(asInt64(m["discount_percent"]))
			if bv, ok := m["discount_matched"].(bool); ok {
				e.DiscountMatched = bv
			}
		} else if cv, ok := h.cfg.ModelRates[id]; ok && cv > 0 {
			e.Rate, e.RateSource = cv, "config"
		}
		out = append(out, e)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"object":          "list",
		"total":           len(out),
		"hidden_custom":   hiddenCustom,
		"hidden_internal": hiddenInternal,
		"data":            out,
		"note":            "仅列出面向用户的官方模型（已过滤你在 TRAE 客户端自行添加的自定义模型，以及 browser_use_subagent 这类内部功能模型）；倍率优先取上游真实值（rate_source=upstream），其次 config.json → model_rates（config），都没有时按 1.0 占位（default）",
	})
}

// adminUsage GET /admin/api/usage：最近的调用记录与汇总（只读，无鉴权）。
// 可选 ?limit=N 控制返回条数（默认返回全部保留记录，最多 usageLogCapacity 条）。
func (h *Handler) adminUsage(w http.ResponseWriter, r *http.Request) {
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"summary": h.stats.summary(),
		"records": h.stats.recent(limit),
	})
}

// asInt64 宽松取整：modelList 的 map 里数值可能是 int / int64 / float64。
func asInt64(v any) int64 {
	switch n := v.(type) {
	case int:
		return int64(n)
	case int64:
		return n
	case float64:
		return int64(n)
	}
	return 0
}

// asString 宽松取字符串。
func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
