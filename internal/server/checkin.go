// checkin.go /admin/api/checkin：一键签到（全账号并发执行）。
//
// 行为对齐 cmd/signin 与 scheduler.RunCheckinNow，区别是按需由面板触发：
//  1. token 临近过期（<2h）先 RefreshToken 并原子落盘，避免签到因 401 失败
//  2. CheckinStatus 查询今日签到状态与可领积分
//  3. 未签到且签到开关开启 → CheckinClaim 领取（限流错误退避后重试一次）
//  4. 重新查积分，remain > 0 且处于冷却的账号自动解冻（Pool.ReenableIfCredits）
//
// 安全纪律：写操作，经 withAdminAuth（Bearer = TW2A_API_KEY）校验；
// 响应只含 uid/nickname/积分等脱敏信息，绝不返回 token。
package server

import (
	"errors"
	"net/http"
	"sync"
	"time"

	"trae2api-web/internal/pool"
	"trae2api-web/internal/upstream"
)

const (
	// checkinRetryDelay 签到限流（9074）后的重试退避时长。
	checkinRetryDelay = 1500 * time.Millisecond
	// checkinMaxParallel 同时进行的签到账号数上限。上游对签到有限流，
	// 全量并发反而容易自伤，故做节流。
	checkinMaxParallel = 4
)

// checkinResult 单账号签到结果。
type checkinResult struct {
	UID      string `json:"uid"`
	Nickname string `json:"nickname,omitempty"`
	// Action 取值：
	//   claimed    本次签到成功
	//   already    今日已签到
	//   no_checkin 签到功能未开启（enable=false）
	//   disabled   账号已被禁用（session dead 等）
	//   no_auth    缺少凭证或 refreshToken 为空
	//   failed     请求失败，原因见 error
	Action  string `json:"action"`
	Credits int64  `json:"checkin_credits,omitempty"` // 签到可领积分
	Remain  int64  `json:"remain"`                    // 签到后最新剩余积分
	// Code 上游业务错误码（如 9074「当前参与用户太多」），仅失败时有值。
	// 上游失败也是 HTTP 200，前端靠它区分限流等场景给出友好提示。
	Code  int    `json:"code,omitempty"`
	Error string `json:"error,omitempty"`
}

// adminCheckin POST /admin/api/checkin：对所有启用账号执行一次签到 + 积分刷新 + 解冻。
func (h *Handler) adminCheckin(w http.ResponseWriter, r *http.Request) {
	st := h.cfg.Pool.List()
	out := make([]checkinResult, len(st))

	// 并发执行（单账号需 2~3 次上游请求），但限制同时在跑的账号数。
	sem := make(chan struct{}, checkinMaxParallel)
	var wg sync.WaitGroup
	for i, s := range st {
		wg.Add(1)
		go func(i int, s pool.Status) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			out[i] = h.checkinOne(s)
		}(i, s)
	}
	wg.Wait()

	var claimed, already, skipped, failed int
	for _, res := range out {
		switch res.Action {
		case "claimed":
			claimed++
		case "already":
			already++
		case "failed":
			failed++
		default:
			skipped++
		}
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"total":      len(out),
		"claimed":    claimed,
		"already":    already,
		"failed":     failed,
		"skipped":    skipped,
		"checked_at": time.Now().Format("2006-01-02 15:04:05"),
		"accounts":   out,
	})
}

// checkinOne 执行单个账号的签到流程，返回逐账号结果。
func (h *Handler) checkinOne(s pool.Status) checkinResult {
	res := checkinResult{UID: s.UID, Nickname: s.Nickname}

	// 禁用的账号跳过（需人工重登恢复，签到救不回来）
	if s.Disabled {
		res.Action = "disabled"
		return res
	}
	a := h.cfg.Pool.AuthByUID(s.UID)
	if a == nil || a.RefreshTokenValue() == "" {
		res.Action = "no_auth"
		res.Error = "缺少凭证或 refreshToken 为空"
		return res
	}

	// 1. token 临近过期先刷新（与 cmd/signin 一致）；刷新失败则本次签到直接判失败。
	if a.NeedsRefresh(2 * time.Hour) {
		if err := h.cfg.Upstream.RefreshToken(a); err != nil {
			res.Action = "failed"
			res.Error = "refresh: " + err.Error()
			return res
		}
		_ = a.SaveAtomic()
	}

	// 2. 查询今日签到状态
	checkedIn, credits, enable, err := h.cfg.Upstream.CheckinStatus(a)
	res.Credits = credits
	switch {
	case err != nil:
		res.Action = "failed"
		res.Error = "checkin status: " + err.Error()
	case checkedIn:
		res.Action = "already"
	case !enable:
		res.Action = "no_checkin"
	default:
		// 3. 领取签到积分；限流（9074 当前登录用户太多）属瞬时错误，退避后重试一次。
		cerr := h.cfg.Upstream.CheckinClaim(a)
		var ce *upstream.CheckinError
		if errors.As(cerr, &ce) && ce.Retryable() {
			time.Sleep(checkinRetryDelay)
			cerr = h.cfg.Upstream.CheckinClaim(a)
		}
		switch {
		case cerr == nil:
			res.Action = "claimed"
		default:
			res.Action = "failed"
			// 业务错误直接用上游中文提示（如 9074「当前参与用户太多，请稍后再试」），
			// 比裸错误码更易读；code 单独暴露便于前端区分限流场景。
			var be *upstream.CheckinError
			if errors.As(cerr, &be) {
				res.Code = be.Code
				res.Error = be.Message
			} else {
				res.Error = "checkin claim: " + cerr.Error()
			}
		}
	}

	// 4. 刷新积分并在有额度时解冻冷却账号（与 scheduler 行为保持一致）
	if remain, qerr := h.cfg.Upstream.UserEntUsage(a); qerr == nil {
		res.Remain = remain
		h.cfg.Pool.ReenableIfCredits(s.UID, remain)
	} else if res.Action != "failed" {
		res.Error = "ent_usage: " + qerr.Error()
	}
	return res
}
