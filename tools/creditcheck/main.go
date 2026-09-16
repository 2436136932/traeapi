// creditcheck 积分核对工具（运维/排查用，不参与服务运行）：
//   - dump 模式：打印每个账号 ide_user_ent_usage 与 user_current_entitlement_list 的
//     原始权益包结构（看「通用/ide」与「work」积分的区分与额度）。
//   - raw 模式：同上，但不解析，直接把两个接口的原始响应落盘到 data/。
//   - chat 模式：直接打上游 llm_utils_chat（SSE），抓 event:notify_usage，
//     看一次真实对话到底扣的是哪种积分、剩余多少。
//
// 用法：go run ./tools/creditcheck [dump|raw|chat|both]
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"traeapi/internal/auth"
	"traeapi/internal/upstream"
)

func main() {
	mode := "both"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	accts := loadAccounts("auths")
	if len(accts) == 0 {
		fmt.Println("no accounts")
		os.Exit(1)
	}
	for _, a := range accts {
		if a.NeedsRefresh(2 * time.Hour) {
			if err := upstream.New().RefreshToken(a); err != nil {
				fmt.Printf("%s refresh: %v\n", a.UID, err)
			}
		}
	}
	if mode == "dump" || mode == "both" || mode == "raw" {
		rawOnly := mode == "raw"
		for _, a := range accts {
			dumpAccount(a, rawOnly)
		}
	}
	if mode == "chat" || mode == "both" {
		chatProbe(accts[0])
	}
}

func loadAccounts(dir string) []*auth.Auth {
	files, _ := filepath.Glob(filepath.Join(dir, "trae-*.json"))
	sort.Strings(files)
	var out []*auth.Auth
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		a, err := auth.Parse(raw)
		if err != nil {
			continue
		}
		a.FilePath = f
		out = append(out, a)
	}
	return out
}

// dumpAccount 打印一个账号的两类额度接口原始结构。
func dumpAccount(a *auth.Auth, rawOnly bool) {
	fmt.Printf("\n================ %s (%s) ================\n", a.UID, a.Nickname)

	raw := ugPost(a, upstream.EpEntUsage)
	raw2 := ugPost(a, "/trae/api/v2/pay/user_current_entitlement_list")
	if rawOnly {
		f1 := fmt.Sprintf("data/raw_ent_%s.json", a.UID)
		f2 := fmt.Sprintf("data/raw_curr_%s.json", a.UID)
		_ = os.WriteFile(f1, raw, 0o600)
		_ = os.WriteFile(f2, raw2, 0o600)
		fmt.Printf("已落盘 %s / %s（%d / %d 字节）\n", f1, f2, len(raw), len(raw2))
		return
	}
	fmt.Println("--- POST /trae/api/v2/pay/ide_user_ent_usage ---")
	var top map[string]any
	_ = json.Unmarshal(raw, &top)
	printPackList(top, "user_entitlement_pack_list")

	fmt.Println("--- POST /trae/api/v2/pay/user_current_entitlement_list ---")
	var top2 map[string]any
	_ = json.Unmarshal(raw2, &top2)
	printPackList(top2, "user_entitlement_pack_list")
	printOtherFields(top2)
}

// printPackList 逐包打印（截断到 700 字符，便于看字段名）。
func printPackList(top map[string]any, key string) {
	packs, _ := top[key].([]any)
	fmt.Printf("包数=%d\n", len(packs))
	for i, p := range packs {
		raw, _ := json.Marshal(p)
		fmt.Printf("  [%d] %s\n", i, trunc(string(raw), 700))
	}
}

// printOtherFields 打印顶层其它标量字段（不含包列表）。
func printOtherFields(top map[string]any) {
	var parts []string
	for k, v := range top {
		if k == "user_entitlement_pack_list" {
			continue
		}
		raw, _ := json.Marshal(v)
		parts = append(parts, k+"="+trunc(string(raw), 120))
	}
	sort.Strings(parts)
	fmt.Println("其它字段: " + strings.Join(parts, " | "))
}

// chatProbe 直连上游发一次最小对话，打印 SSE 事件与计费事件。
func chatProbe(a *auth.Auth) {
	fmt.Printf("\n================ 真实对话计费探测 (%s) ================\n", a.UID)
	body, _ := json.Marshal(map[string]any{
		"function":    "solo_work_lite",
		"config_name": "glm-5.2",
		"model":       "glm-5.2",
		"stream":      true,
		"messages": []any{map[string]any{
			"role":    "user",
			"content": []any{map[string]any{"type": "text", "text": "只回复两个字：收到"}},
		}},
	})
	req, err := http.NewRequest(http.MethodPost, upstream.AgentHost+upstream.EpChat, bytes.NewReader(body))
	if err != nil {
		fmt.Println("req:", err)
		return
	}
	upstream.SOLOHeaders(req, a, true)
	cli := &http.Client{Timeout: 180 * time.Second}
	resp, err := cli.Do(req)
	if err != nil {
		fmt.Println("do:", err)
		return
	}
	defer resp.Body.Close()
	fmt.Printf("http=%d\n", resp.StatusCode)
	if resp.StatusCode != 200 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		fmt.Println("body:", trunc(string(raw), 300))
		return
	}
	br := bufio.NewReader(resp.Body)
	event := ""
	seen := map[string]int{}
	for {
		line, err := br.ReadString('\n')
		line = strings.TrimRight(line, "\r\n")
		switch {
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			seen[event]++
			// output 事件多且长，只标记；其余事件全量打印，便于看清计费字段在哪。
			if event == "output" {
				if seen[event] == 1 {
					fmt.Printf("[output] (增量内容，已省略) count=...\n")
				}
			} else {
				fmt.Printf("[%s] %s\n", event, trunc(data, 1200))
			}
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			fmt.Println("read:", err)
			break
		}
	}
	fmt.Printf("事件统计: ")
	for k, v := range seen {
		fmt.Printf("%s=%d ", k, v)
	}
	fmt.Println()
}

// ugPost 向 api.trae.cn 发一次空 body POST，返回响应体。
func ugPost(a *auth.Auth, path string) []byte {
	req, err := http.NewRequest(http.MethodPost, upstream.UgHost+path, bytes.NewReader([]byte("{}")))
	if err != nil {
		return nil
	}
	upstream.UgHeaders(req, a)
	cli := &http.Client{Timeout: 30 * time.Second}
	resp, err := cli.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if resp.StatusCode != 200 {
		fmt.Printf("http=%d body=%s\n", resp.StatusCode, trunc(string(raw), 200))
	}
	return raw
}

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}