// headers.go SOLO 三类请求头：对话（SOLOHeaders）/ ug（UgHeaders）/ oauth（OAuthHeaders）。
package upstream

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"net/http"
	"strings"

	"traeapi/internal/auth"
)

const clientUA = "Trae/" + IdeVersion

// SOLOHeaders 设置 llm_utils_chat / get_detail_param 所需的 SOLO 专属头。
// 规则来自 SPEC §1 SOLO headers（实测必须）。
func SOLOHeaders(req *http.Request, a *auth.Auth, stream bool) {
	req.Header.Set("Content-Type", "application/json")
	if stream {
		req.Header.Set("Accept", "text/event-stream")
	} else {
		req.Header.Set("Accept", "application/json")
	}
	req.Header.Set("User-Agent", clientUA)
	at := a.JWT() // 读锁快照，防与 RefreshToken 写并发竞态
	req.Header.Set("Authorization", "Cloud-IDE-JWT "+at)
	req.Header.Set("X-Cloudide-Token", at)
	req.Header.Set("X-Ide-Token", at)
	if a.UID != "" {
		req.Header.Set("X-Uid", a.UID)
	}
	req.Header.Set("X-App-Id", AppID)
	req.Header.Set("X-App-Version", "default")
	req.Header.Set("X-Ide-Version", IdeVersion)
	req.Header.Set("X-Ide-Version-Code", IdeVersionCode)
	req.Header.Set("X-App-Version-Code", IdeVersionCode)
	req.Header.Set("X-Ide-Version-Type", "stable")
	req.Header.Set("X-Device-Type", "windows")
	req.Header.Set("X-OS-Version", OSVersion)
	req.Header.Set("X-Device-Brand", DeviceBrand)
	req.Header.Set("Request-Traffic-Type", "prod")
	if a.MachineID != "" {
		req.Header.Set("X-Machine-Id", a.MachineID)
	}
	if a.DeviceID != "" {
		req.Header.Set("X-Device-Id", a.DeviceID)
	}
}

// UgHeaders 设置签到/积分（api.trae.cn）所需头。
func UgHeaders(req *http.Request, a *auth.Auth) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", clientUA)
	req.Header.Set("Authorization", "Cloud-IDE-JWT "+a.JWT()) // 读锁快照
	req.Header.Set("X-User-Region", "CN")
	// 必须 16 位数字设备号，否则签到被风控拒绝（见 ugDeviceID）
	req.Header.Set("X-Device-Id", ugDeviceID(a))
}

// ugDeviceID 返回 ug 接口（签到/积分）应使用的 x-device-id。
//
// 实测（2026-09-16，uid 2666736248956905，与时段无关、可稳定复现）：
//   - 用登录流程落盘的 32 位十六进制 GUID（login.sh 自生成）调
//     /trae/api/v2/ug/checkin_credits/claim → HTTP 200 + code 9074「当前参与用户太多，请稍后再试」
//   - 同一账号把该头换成任意 16 位数字 → 立即 code 0 签到成功
//   - 完全不发该头 → code 9004「The submitted order parameters are incorrect」
//   （status / ide_user_ent_usage 对这两种形态都放行，只有 claim 校验。）
//
// 结论：上游风控要求 x-device-id 是 16 位数字的「Aha 设备号」，签到时用 GUID
// 会被判为异常设备而返回 9074（文案误导为「参与用户太多」）。故这里为每个账号
// 派生一个稳定、互不相同的 16 位数字设备号：同账号每天都是同一设备号，
// 不同账号各自独立（规避「一台设备只能签一个账号」的限制）。
// auth 文件里的 deviceId 若已是 16 位数字则直接沿用，便于人工指定/更换。
func ugDeviceID(a *auth.Auth) string {
	if len(a.DeviceID) == 16 && strings.Trim(a.DeviceID, "0123456789") == "" {
		return a.DeviceID
	}
	seed := a.UID
	if seed == "" {
		seed = a.JWT() // 无 uid 时退化用 token 派生，仍保证稳定
	}
	sum := sha256.Sum256([]byte(ugDeviceSeedPrefix + seed))
	return fmt.Sprintf("%016d", binary.BigEndian.Uint64(sum[:8])%10000000000000000)
}

// ugDeviceSeedPrefix 派生设备号的域分隔前缀，避免与其他 sha256 用途撞语义。
const ugDeviceSeedPrefix = "traeapi-checkin-device:"

// OAuthHeaders 设置 ExchangeToken / GetUserInfo 所需头（无签名，仅 UA）。
func OAuthHeaders(req *http.Request) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", clientUA)
}
