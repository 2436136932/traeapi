// stats.go 调用记录：内存环形缓冲，供面板「调用记录」页展示。
//
// 设计约束：
//   - 只存内存（重启即清空），不落盘；只记录元信息，不含 prompt / 回复正文，
//     避免用户对话内容留存在磁盘上。
//   - 固定容量覆盖式写入，长时间运行内存占用恒定。
package server

import (
	"sync"
	"time"
)

// usageRecord 单次 /v1/chat/completions 调用的元信息。
type usageRecord struct {
	Time             string `json:"time"`
	Model            string `json:"model"`
	UID              string `json:"uid,omitempty"`
	Nickname         string `json:"nickname,omitempty"`
	Stream           bool   `json:"stream"`
	OK               bool   `json:"ok"`
	PromptTokens     int    `json:"prompt_tokens"`
	CompletionTokens int    `json:"completion_tokens"`
	TotalTokens      int    `json:"total_tokens"`
	DurationMS       int64  `json:"duration_ms"`
	ErrCode          string `json:"err_code,omitempty"`
	ErrMsg           string `json:"err_msg,omitempty"`
}

// usageLog 固定容量调用记录环形缓冲（并发安全）。
type usageLog struct {
	mu       sync.Mutex
	capacity int
	buf      []usageRecord
	next     int   // 下一个写入槽位
	filled   int   // 当前已写入条数（判满用）
	total    int64 // 累计调用数
	okCount  int64
	created  time.Time
}

// usageLogCapacity 调用记录保留条数上限（环形缓冲容量）。
const usageLogCapacity = 200

// usageInt 取 usage map 中的整数字段（JSON 解码后数值为 float64）。
func usageInt(m map[string]any, key string) int {
	switch v := m[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	}
	return 0
}

func newUsageLog(capacity int) *usageLog {
	if capacity <= 0 {
		capacity = 200
	}
	return &usageLog{
		capacity: capacity,
		buf:      make([]usageRecord, capacity),
		created:  time.Now(),
	}
}

// add 追加一条记录（超出容量时覆盖最旧的一条）。
func (l *usageLog) add(r usageRecord) {
	if r.Time == "" {
		r.Time = time.Now().Format("2006-01-02 15:04:05")
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.buf[l.next] = r
	l.next = (l.next + 1) % l.capacity
	if l.filled < l.capacity {
		l.filled++
	}
	l.total++
	if r.OK {
		l.okCount++
	}
}

// recent 返回最近 n 条记录，最新在前；n <= 0 时返回全部保留记录。
func (l *usageLog) recent(n int) []usageRecord {
	l.mu.Lock()
	defer l.mu.Unlock()
	if n <= 0 || n > l.filled {
		n = l.filled
	}
	out := make([]usageRecord, 0, n)
	for i := 0; i < n; i++ {
		// 从最新写入的位置往前取；加上 2*capacity 保证取模前恒为正
		idx := (l.next - 1 - i + l.capacity*2) % l.capacity
		out = append(out, l.buf[idx])
	}
	return out
}

// summary 汇总统计（对当前保留的记录求和，累计调用数单独给出）。
func (l *usageLog) summary() map[string]any {
	l.mu.Lock()
	defer l.mu.Unlock()
	var prompt, completion, total, durSum int64
	for i := 0; i < l.filled; i++ {
		r := l.buf[i]
		prompt += int64(r.PromptTokens)
		completion += int64(r.CompletionTokens)
		total += int64(r.TotalTokens)
		durSum += r.DurationMS
	}
	var avg int64
	if l.filled > 0 {
		avg = durSum / int64(l.filled)
	}
	return map[string]any{
		"total_calls":       l.total,
		"ok_calls":          l.okCount,
		"failed_calls":      l.total - l.okCount,
		"kept":              l.filled,
		"capacity":          l.capacity,
		"avg_duration_ms":   avg,
		"prompt_tokens":     prompt,
		"completion_tokens": completion,
		"total_tokens":      total,
		"since":             l.created.Format("2006-01-02 15:04:05"),
	}
}
