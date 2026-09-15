// Package scheduler 定时任务：每日签到 + token 预刷新。
// 签到成功后重新查积分，积分 > 0 的冷却账号自动解冻。
package scheduler

import (
	"context"
	"errors"
	"log"
	"time"

	"traeapi/internal/pool"
	"traeapi/internal/upstream"
)

// Config 调度器依赖。
type Config struct {
	Pool         *pool.Pool
	Upstream     *upstream.Client
	CheckinHour  int           // 每日签到小时，默认 9
	RefreshHours []int         // token 预刷新小时，默认 [3]
	RefreshSkew  time.Duration // 预刷新窗口，默认 24h
	// CheckinRetry 签到未能全部成功（如上游 9074「当前参与用户太多」）后的
	// 自动重试间隔；0 表示不重试。
	CheckinRetry time.Duration
}

// maxCheckinRetries 单个签到周期内最多自动重试次数，
// 避免上游持续拒绝时无休止地刷请求。
const maxCheckinRetries = 12

// Scheduler 调度器。
type Scheduler struct {
	cfg Config
}

// New 构建。
func New(cfg Config) *Scheduler {
	if cfg.CheckinHour < 0 {
		cfg.CheckinHour = 9
	}
	if len(cfg.RefreshHours) == 0 {
		cfg.RefreshHours = []int{3}
	}
	if cfg.RefreshSkew <= 0 {
		cfg.RefreshSkew = 24 * time.Hour
	}
	return &Scheduler{cfg: cfg}
}

// nextFire 返回 now 之后最近的一个整点触发时间；hours 为本地小时（0-23）。
func nextFire(now time.Time, hours []int) time.Time {
	var earliest time.Time
	for _, h := range hours {
		t := time.Date(now.Year(), now.Month(), now.Day(), h, 0, 0, 0, now.Location())
		if !t.After(now) {
			t = t.Add(24 * time.Hour)
		}
		if earliest.IsZero() || t.Before(earliest) {
			earliest = t
		}
	}
	return earliest
}

// Run 主循环，阻塞直到 ctx 取消。
//
// 除整点触发外，若签到未能全部成功（上游 9074「当前参与用户太多」在高峰很常见），
// 会按 cfg.CheckinRetry 的间隔自动重试，最多 maxCheckinRetries 次。
func (s *Scheduler) Run(ctx context.Context) {
	all := append(append([]int{}, s.cfg.RefreshHours...), s.cfg.CheckinHour)
	var (
		retryTimer *time.Timer
		retryCh    <-chan time.Time
		retries    int
	)
	stopRetry := func() {
		if retryTimer != nil {
			retryTimer.Stop()
			retryTimer = nil
		}
		retryCh = nil
		retries = 0
	}

	// 启动兜底：服务可能在当日签到时刻之后才启动（或刚重启），
	// 此时立即补签一次；未全部成功则直接进入重试流程。
	// 若当前尚未到签到时刻，则不做任何请求，等整点触发。
	if time.Now().Hour() >= s.cfg.CheckinHour {
		if !s.RunCheckinNow() && s.cfg.CheckinRetry > 0 {
			log.Printf("checkin: 启动检查发现未签到成功，%s 后自动重试", s.cfg.CheckinRetry)
			retryTimer = time.NewTimer(s.cfg.CheckinRetry)
			retryCh = retryTimer.C
		}
	}
	for {
		next := nextFire(time.Now(), all)
		timer := time.NewTimer(time.Until(next))
		select {
		case <-ctx.Done():
			timer.Stop()
			stopRetry()
			return
		case <-timer.C:
			h := time.Now().Hour()
			if contains(s.cfg.RefreshHours, h) {
				s.RunRefreshNow()
			}
			if s.cfg.CheckinHour == h {
				stopRetry()
				if !s.RunCheckinNow() && s.cfg.CheckinRetry > 0 {
					log.Printf("checkin: 有账号未签到成功，%s 后自动重试", s.cfg.CheckinRetry)
					retryTimer = time.NewTimer(s.cfg.CheckinRetry)
					retryCh = retryTimer.C
				}
			}
		case <-retryCh:
			timer.Stop() // 本轮整点定时器未触发，避免堆积
			retries++
			if s.RunCheckinNow() {
				log.Printf("checkin: 重试第 %d 次后全部成功", retries)
				stopRetry()
				continue
			}
			if retries >= maxCheckinRetries {
				log.Printf("checkin: 已重试 %d 次仍未全部成功，本次停止重试", retries)
				stopRetry()
				continue
			}
			retryTimer.Reset(s.cfg.CheckinRetry)
		}
	}
}

func contains(hours []int, h int) bool {
	for _, v := range hours {
		if v == h {
			return true
		}
	}
	return false
}

// RunCheckinNow 立即对所有账号执行签到 + 积分刷新 + 解冻。
// 冷却中的账号也参与（签到就是为了解冻它们）；禁用的跳过。
// 返回是否全部账号都已签到成功——false 表示有账号失败（如上游 9074 限流）
// 或仍处于未签到状态，调用方可据此安排自动重试。
func (s *Scheduler) RunCheckinNow() (allDone bool) {
	allDone = true
	for _, st := range s.cfg.Pool.List() {
		if st.Disabled {
			continue
		}
		a := s.cfg.Pool.AuthByUID(st.UID)
		if a == nil || a.RefreshTokenValue() == "" {
			continue
		}
		// 签到（status → 未签到则 claim）
		checkedIn, _, enable, err := s.cfg.Upstream.CheckinStatus(a)
		switch {
		case err != nil:
			log.Printf("checkin status %s: %v", st.UID, err)
			allDone = false
		case checkedIn:
			log.Printf("checkin %s: already checked in", st.UID)
		case !enable:
			// 该账号签到功能未开启，不视为失败
		default:
			if err := s.cfg.Upstream.CheckinClaim(a); err != nil {
				log.Printf("checkin claim %s: %v", st.UID, err)
				allDone = false
			} else {
				log.Printf("checkin %s: ok", st.UID)
			}
		}
		// 查积分 + 解冻
		remain, err := s.cfg.Upstream.UserEntUsage(a)
		if err != nil {
			log.Printf("ent-usage %s: %v", st.UID, err)
			continue
		}
		s.cfg.Pool.ReenableIfCredits(st.UID, remain)
	}
	return allDone
}

// RunRefreshNow 立即对所有账号刷新 token；session 失效的自动禁用。
func (s *Scheduler) RunRefreshNow() {
	for _, st := range s.cfg.Pool.List() {
		if st.Disabled {
			continue
		}
		a := s.cfg.Pool.AuthByUID(st.UID)
		if a == nil || a.RefreshTokenValue() == "" {
			continue
		}
		if !a.NeedsRefresh(s.cfg.RefreshSkew) {
			continue
		}
		if err := s.cfg.Upstream.RefreshToken(a); err != nil {
			log.Printf("refresh %s: %v", st.UID, err)
			var ue *upstream.Error
			if errors.As(err, &ue) && ue.Kind == upstream.ErrSessionDead {
				s.cfg.Pool.Disable(st.UID, "session dead")
			}
			continue
		}
		if err := a.SaveAtomic(); err != nil {
			log.Printf("refresh %s save: %v", st.UID, err)
		}
	}
}
