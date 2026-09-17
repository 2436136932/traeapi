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
	// 设备号：签到时上游风控按它判定，必须提供（缺失 → 9004）
	req.Header.Set("X-Device-Id", ugDeviceID(a))
}

// ugDeviceID 返回 ug 接口应使用的 x-device-id（首选值）。
//
// 实测（2026-09-16 / 09-17，两个账号各一次「当天首次 claim」对照实验）：
//   - 完全不发该头 → code 9004「order parameters are incorrect」；
//   - 账号当天**首次** claim 时，9074 与设备号取值强相关：
//     x-device-id = uid → code 0 成功（两个账号、两天，都是首选即成功）；
//     32 位十六进制 GUID（登录流程自生成）、sha256 派生 16 位、随机 16 位数字 → 9074；
//   - 账号当天已签到后，claim 幂等返回 code 0，**不再校验设备号**
//     （这正是 09-16 误判「任意 16 位数字都行」的原因：当时那次随机值尝试其实吃的是幂等结果）。
//
// 因此首选 uid（TRAE 账号 id 本身即 16 位数字）；auth 文件里若是人工写的 16 位数字
// deviceId 则优先（便于换绑）；再不行才用派生值兜底。候选顺序见 checkinDevicePlan。
func ugDeviceID(a *auth.Auth) string {
	if num16(a.DeviceID) {
		return a.DeviceID
	}
	if num16(a.UID) {
		return a.UID
	}
	return derivedUGDeviceID(a)
}

// checkinDevicePlan 返回 claim 时依次尝试的 x-device-id 候选（去重、非空）。
// 首选 uid（实测唯一稳定通过的取值），随后是账号登录时的 GUID 与派生值兜底。
func checkinDevicePlan(a *auth.Auth) []string {
	var out []string
	add := func(v string) {
		if v == "" {
			return
		}
		for _, x := range out {
			if x == v {
				return
			}
		}
		out = append(out, v)
	}
	add(ugDeviceID(a))        // 首选：人工指定值 / uid
	add(uidIf16(a))           // uid 始终保留为候选（实测唯一稳定通过的取值）
	add(a.DeviceID)           // 账号登录时记录的设备号（32 位十六进制）
	add(derivedUGDeviceID(a)) // 派生兜底（稳定且各账号互异）
	return out
}

// uidIf16 返回 16 位数字的 uid，否则空串（非 16 位时不能当设备号用）。
func uidIf16(a *auth.Auth) string {
	if num16(a.UID) {
		return a.UID
	}
	return ""
}

// derivedUGDeviceID 由账号 id 派生一个稳定的 16 位数字设备号（兜底用）。
func derivedUGDeviceID(a *auth.Auth) string {
	seed := a.UID
	if seed == "" {
		seed = a.JWT() // 无 uid 时退化用 token 派生，仍保证稳定
	}
	sum := sha256.Sum256([]byte(ugDeviceSeedPrefix + seed))
	return fmt.Sprintf("%016d", binary.BigEndian.Uint64(sum[:8])%10000000000000000)
}

// num16 判断是否为 16 位纯数字（TRAE 风控设备号的形态）。
func num16(s string) bool { return len(s) == 16 && strings.Trim(s, "0123456789") == "" }

// ugDeviceSeedPrefix 派生设备号的域分隔前缀，避免与其他 sha256 用途撞语义。
const ugDeviceSeedPrefix = "traeapi-checkin-device:"

// OAuthHeaders 设置 ExchangeToken / GetUserInfo 所需头（无签名，仅 UA）。
func OAuthHeaders(req *http.Request) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", clientUA)
}
