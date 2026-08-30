import type { Lang } from "../i18n"

interface Props {
  lang: Lang
}

/**
 * Data collection has been discontinued.
 *
 * This page used to accept Claude Code / Codex CLI session transcripts
 * (`.jsonl`) for benchmark and ablation research. Every collection channel —
 * session upload, rating feedback, heartbeat telemetry and the leaderboard —
 * has been removed from both the client hooks and the server, so there is no
 * endpoint left to upload to.
 *
 * The component is kept (rather than deleted) so the existing `/contribute.html`
 * route resolves to an honest notice instead of a dead link or a broken form.
 */
export default function Contribute({ lang }: Props) {
  const L = (zh: string, en: string, ja?: string) =>
    lang === "zh" ? zh : lang === "ja" ? (ja ?? en) : en

  const removed = [
    {
      zh: "对话记录上传",
      en: "Session transcript upload",
      ja: "セッション記録のアップロード",
    },
    { zh: "评分反馈上报", en: "Rating feedback", ja: "評価フィードバック送信" },
    { zh: "心跳 telemetry", en: "Heartbeat telemetry", ja: "ハートビート計測" },
    { zh: "PUA 排行榜", en: "PUA leaderboard", ja: "PUA リーダーボード" },
  ]

  return (
    <main
      style={{
        maxWidth: "44rem",
        margin: "0 auto",
        padding: "4rem 1.5rem",
        lineHeight: 1.7,
      }}
    >
      <h1 style={{ fontSize: "1.75rem", fontWeight: 700, marginBottom: "1rem" }}>
        {L("数据收集已停止", "Data collection discontinued", "データ収集は終了しました")}
      </h1>

      <p style={{ marginBottom: "1.5rem" }}>
        {L(
          "PUA Skill 已移除全部联网上报功能。客户端 hook 和服务端接收接口都已删除，没有任何数据会离开你的机器。",
          "PUA Skill no longer reports anything over the network. Both the client-side hooks and the server-side endpoints have been removed, so no data leaves your machine.",
          "PUA Skill はネットワーク送信機能をすべて削除しました。クライアント側フックもサーバー側エンドポイントも削除済みで、データが端末外に出ることはありません。"
        )}
      </p>

      <h2 style={{ fontSize: "1.05rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        {L("已移除的通道", "Removed channels", "削除されたチャネル")}
      </h2>
      <ul style={{ paddingLeft: "1.25rem", marginBottom: "1.5rem" }}>
        {removed.map((item) => (
          <li key={item.en} style={{ marginBottom: "0.25rem" }}>
            {L(item.zh, item.en, item.ja)}
          </li>
        ))}
      </ul>

      <p style={{ marginBottom: "1.5rem" }}>
        {L(
          "任务结束时的反馈问卷仍然保留，但只写入本机 ~/.pua/feedback.jsonl。不想看到它，运行 /pua:offline 或在 ~/.pua/config.json 里设 feedback_frequency: 0。",
          "The end-of-task feedback prompt still exists, but it only appends to ~/.pua/feedback.jsonl on your own machine. To silence it, run /pua:offline or set feedback_frequency: 0 in ~/.pua/config.json.",
          "タスク終了時のフィードバックは残っていますが、ローカルの ~/.pua/feedback.jsonl に書き込むだけです。非表示にするには /pua:offline を実行するか、~/.pua/config.json で feedback_frequency: 0 を設定してください。"
        )}
      </p>

      <p>
        <a href="/">
          {L("← 返回首页", "← Back to home", "← ホームに戻る")}
        </a>
      </p>
    </main>
  )
}
