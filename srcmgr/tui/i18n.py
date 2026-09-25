"""Chinese-first message table for the menu UI.

Everything the beginner UI shows goes through :func:`tr`, so a translation is a matter of
adding one dictionary — no code changes.  The key is a short, stable id; the value may
contain ``{placeholders}`` formatted with ``tr("key", **values)``.

Two rules keep this honest:

* **No jargon.**  "APT（系统软件）" instead of "APT sources.list deb822"; "下载源" instead of
  "repository".  If a sentence needs Linux knowledge to parse, it belongs in the CLI.
* **Always say what to do next.**  Error strings come in pairs: what happened, and the
  button the user should press.  See :func:`friendly_error`.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_LANGUAGE = "zh"

#: ``{language: {key: text}}``.  ``zh`` is complete; ``en`` falls back to ``zh`` per key,
#: so an incomplete translation degrades gracefully instead of showing blank labels.
MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        # -- window chrome ---------------------------------------------------
        "app.title": "源管理器 Source Manager",
        "app.subtitle": "把软件的下载源换成更快、更稳的镜像",
        "app.refresh": "刷新",
        "app.quit": "退出",
        "app.language": "English",
        # -- home screen -----------------------------------------------------
        "home.question": "你想管理哪个软件的下载源？",
        "home.loading": "正在检查这台电脑上装了哪些软件…",
        "home.no_managers": "没有找到可以管理的软件源。",
        "home.admin_ok": "管理员权限 ✓",
        "home.admin_missing": "管理员权限 ✗",
        "home.locked_summary": "已锁定 {count} 个生态，自动操作会跳过它们",
        "home.col_software": "软件",
        "home.col_state": "状态",
        "home.col_current": "现在用的是",
        "home.col_lock": "锁定",
        "home.hint": "↑↓ 选择    Enter 进入    鼠标可点击    L 锁定/解锁    Q 退出",
        "home.health_ok": "正常",
        "home.health_warn": "有点慢",
        "home.health_bad": "{count} 个源打不开",
        "home.health_unknown": "未检测",
        "home.not_detected": "这台电脑没装",
        "home.no_source": "还没设置",
        "home.offline_note": "⚠ 当前处于离线模式（SRCMGR_OFFLINE=1），测速结果都会显示为打不开。",
        "eco.showing_all": "已显示全部 {count} 个下载源",
        # -- lock ------------------------------------------------------------
        "lock.lock": "点这里锁定",
        "lock.unlock": "已锁定（点击解锁）",
        "lock.locked_row": "🔒 已锁定",
        "lock.unlocked_row": "🔓 可修改",
        "lock.column": "锁定",
        "lock.now_locked": "{name} 已锁定，不会再被自动修改",
        "lock.now_unlocked": "{name} 已解锁，可以切换下载源了",
        "lock.blocked_title": "{name} 已锁定",
        "lock.blocked_body": (
            "锁定状态下不会修改它的下载源，包括“一键换最快源”。\n"
            "如果你想换，请先解锁。"
        ),
        "lock.blocked_unlock_and_switch": "解锁并切换",
        "lock.blocked_cancel": "取消",
        "lock.title_locked": "🔒 {name}｜已锁定",
        "lock.explain": "锁定的生态不会被任何自动操作修改（连“一键换最快源”也会跳过）。",
        # -- bulk actions ----------------------------------------------------
        "action.scan": "🩺 一键体检（测所有源的网速）",
        "action.fastest": "⚡ 一键换到最快源",
        "action.undo": "↩️ 撤销上一次修改",
        "action.history": "📋 修改记录",
        "action.backups": "🗂 备份与还原",
        "action.admin": "🔑 提前解锁管理员权限",
        "action.quit": "🚪 退出",
        "scan.running": "正在测试每个下载源的速度，请稍等…",
        "scan.done": "体检完成：{ok} 个正常，{bad} 个有问题",
        "scan.done_all_ok": "体检完成：所有下载源都正常",
        "scan.need_admin": "测试速度不需要管理员权限。",
        "fastest.running": "正在挑选最快的下载源…",
        "fastest.confirm_title": "要换到最快源吗？",
        "fastest.confirm_body": (
            "将要修改 {count} 个软件：\n{changes}\n\n"
            "修改前会自动备份，随时可以用“撤销”还原。"
        ),
        "fastest.confirm_ok": "开始更换",
        "fastest.done": "已更换 {count} 个软件，跳过 {skipped} 个",
        "fastest.none": "没有需要更换的（已经是最快的，或者都被锁定了）",
        "fastest.skipped_locked": "跳过了锁定的：{names}",
        "fastest.failed": "有 {count} 个没能更换：{names}",
        "undo.nothing": "没有可以撤销的修改。",
        "undo.confirm_title": "要撤销上一次修改吗？",
        "undo.confirm_body": "将把 {name} 恢复到修改之前的样子。",
        "undo.done": "已撤销：{message}",
        "history.title": "修改记录",
        "history.empty": "还没有任何修改记录。",
        "history.col_when": "时间",
        "history.col_action": "操作",
        "history.col_manager": "软件",
        "history.col_detail": "详情",
        "backups.title": "备份与还原",
        "backups.intro": "每次修改前的状态都会自动备份，选一个时间点即可还原。",
        "backups.empty": "还没有备份。第一次修改后就会自动生成。",
        "backups.col_time": "时间",
        "backups.col_manager": "软件",
        "backups.col_files": "文件数",
        "backups.restore": "还原到这个备份",
        "backups.confirm_title": "要还原吗？",
        "backups.confirm_body": "将把 {name} 恢复到 {when} 的状态。",
        "backups.done": "已还原：{message}",
        # -- ecosystem screen ------------------------------------------------
        "eco.current": "现在用的是",
        "eco.choose": "想换成哪一个？（按网速从快到慢）",
        "eco.custom": "✏️ 自己输入网址…",
        "eco.custom_prompt": "请输入下载源网址",
        "eco.custom_invalid": "这个网址看起来不对，要以 http:// 或 https:// 开头。",
        "eco.switch": "切换",
        "eco.rescan": "🔍 重新测速",
        "eco.details": "📄 查看详情",
        "eco.undo": "↩️ 撤销上次修改",
        "eco.backups": "🗂 还原到某个备份",
        "eco.switching": "正在切换到 {name}…",
        "eco.switched": "已经切换到 {name}（{url}）。如果不好用，按“撤销”。",
        "eco.already": "已经在用 {name} 了。",
        "eco.none_available": "这个软件暂时没有可切换的下载源。",
        "eco.locked_notice": "这个软件已锁定，不能切换。",
        "eco.footer": "↑↓ 选择    Enter 切换    Esc 返回    L 锁定/解锁",
        "eco.no_health": "还没测速",
        "eco.testing": "正在测速…",
        "eco.probe_failed": "打不开",
        # -- admin / sudo ----------------------------------------------------
        "admin.needed_title": "需要管理员密码",
        "admin.needed_body": (
            "修改系统软件的下载源需要管理员权限（就是安装系统时设置的密码）。\n"
            "密码只用于这一次操作，不会被保存。"
        ),
        "admin.password": "请输入密码",
        "admin.remember": "本次运行内不再询问",
        "admin.ok": "管理员权限已就绪",
        "admin.bad_password": "密码不对，请再试一次。",
        "admin.cancelled": "已取消。用户级软件（Python、Node.js 等）的下载源不需要密码，仍可修改。",
        "admin.skipped": "{count} 个需要管理员权限的软件被跳过。",
        "admin.cannot_prompt": "无法在这里输入密码，请改用命令行：srcmgr --cli …",
        # -- onboarding ------------------------------------------------------
        "onboard.title": "欢迎使用源管理器",
        "onboard.body": (
            "这个工具帮你把各种软件的下载地址，换成国内更快的镜像。\n\n"
            "• 上下键选择，回车进入，鼠标也能点\n"
            "• 修改前会自动备份，随时可以点“撤销”\n"
            "• 觉得哪个软件已经调好了，就点它右边的 🔓 锁定，以后不会被自动改动"
        ),
        "onboard.ok": "知道了，开始使用",
        "onboard.never": "不再显示",
        # -- generic ---------------------------------------------------------
        "common.back": "← 返回",
        "common.cancel": "取消",
        "common.close": "关闭",
        "common.yes": "确定",
        "common.no": "取消",
        "common.ok": "好的",
        "common.none": "无",
        "common.failed": "操作失败",
        "common.unknown": "未知",
        "error.title": "出问题了",
    },
    "en": {
        "app.title": "Source Manager",
        "app.subtitle": "Switch every software source to a faster, more reliable mirror",
        "home.question": "Which software's download source do you want to manage?",
        "home.loading": "Checking what is installed on this computer…",
        "home.admin_ok": "admin rights ✓",
        "home.admin_missing": "admin rights ✗",
        "home.hint": "↑↓ select    Enter open    mouse works    L lock/unlock    Q quit",
        "action.scan": "🩺 Health check (test every mirror)",
        "action.fastest": "⚡ Switch to the fastest mirrors",
        "action.undo": "↩️ Undo the last change",
        "action.quit": "🚪 Quit",
        "lock.lock": "click to lock",
        "lock.unlock": "locked (click to unlock)",
        "lock.now_locked": "{name} is locked and will not be changed automatically",
        "lock.now_unlocked": "{name} is unlocked",
        "eco.custom": "✏️ Type a URL…",
        "common.cancel": "Cancel",
        "common.yes": "OK",
    },
}


def current_language() -> str:
    """Language for this run: ``SRCMGR_LANG`` overrides the config default."""
    override = os.environ.get("SRCMGR_LANG", "").strip().lower()
    if override in MESSAGES:
        return override
    return DEFAULT_LANGUAGE


def tr(key: str, language: str | None = None, **values: Any) -> str:
    """Translate ``key``; unknown keys return the key itself (never blank)."""
    lang = language or current_language()
    table = MESSAGES.get(lang, {})
    text = table.get(key) or MESSAGES[DEFAULT_LANGUAGE].get(key) or key
    if values:
        try:
            return text.format(**values)
        except (KeyError, IndexError):  # pragma: no cover - defensive
            return text
    return text


#: How a technical failure should be described to a beginner: a plain sentence plus the
#: action that fixes it.  Matched case-insensitively against the raw error text.
_ERROR_HINTS: tuple[tuple[tuple[str, ...], tuple[str, str]], ...] = (
    (
        ("locked", "已锁定"),
        ("这个软件被锁定了，所以没有被修改。", "点上锁图标解锁后再试。"),
    ),
    (
        ("permission", "sudo", "root", "eacces", "permission denied"),
        ("需要管理员权限。", "点“解锁管理员权限”，输入开机密码后再试。"),
    ),
    (
        ("timeout", "timed out", "超时"),
        ("网络连接太慢或对方没有回应。", "换一个下载源试试，或检查网络。"),
    ),
    (
        ("connection refused", "unreachable", "network is unreachable", "name or service not known"),
        ("这个下载源现在打不开。", "换一个镜像即可，体检功能会告诉你哪个最快。"),
    ),
    (
        ("tls", "ssl", "certificate"),
        ("这个下载源的安全证书有问题。", "换一个镜像；如果必须用它，请改用命令行加参数。"),
    ),
    (
        ("not found", "no such file", "missing"),
        ("找不到要修改的文件。", "可能是这个软件还没配置过，先进入它一次再试。"),
    ),
    (
        ("no space", "disk full"),
        ("磁盘空间不足。", "清理一些空间后再试。"),
    ),
)


def friendly_error(raw: str, language: str | None = None) -> tuple[str, str]:
    """Turn a technical error into ``(what happened, what to do)`` in plain Chinese."""
    text = (raw or "").strip()
    lowered = text.lower()
    for needles, (what, todo) in _ERROR_HINTS:
        if any(needle in lowered for needle in needles):
            detail = f"（原始信息：{text}）" if text else ""
            return f"{what}{detail}", todo
    fallback = tr("common.failed", language)
    return (f"{fallback}：{text}" if text else fallback, tr("common.ok", language))


def action_label(kind: str) -> str:
    """Human word for a history/backup action id (``add``, ``rollback``, …)."""
    table = {
        "add": "添加下载源",
        "enable": "启用下载源",
        "disable": "停用下载源",
        "remove": "删除下载源",
        "update": "修改下载源",
        "rollback": "撤销修改",
        "restore": "还原备份",
        "backup": "创建备份",
        "lock": "锁定",
        "unlock": "解锁",
        "fallback": "自动换源",
        "refresh": "刷新缓存",
        "test-native": "测试",
    }
    return table.get(kind, kind)


__all__ = [
    "DEFAULT_LANGUAGE",
    "MESSAGES",
    "action_label",
    "current_language",
    "friendly_error",
    "tr",
]
