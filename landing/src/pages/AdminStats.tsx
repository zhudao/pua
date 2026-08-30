import type { Lang } from "../i18n"

interface Props {
  lang: Lang
}

/**
 * Admin telemetry dashboard — retired.
 *
 * This page rendered install and session counts sourced from the heartbeat
 * telemetry endpoint. That endpoint was removed together with every other
 * data-collection channel, so there is no data source left to render. The
 * component is kept only so the existing admin route still resolves and
 * type-checks; it must not regain a data fetch.
 */
export default function AdminStats({ lang }: Props) {
  const L = (zh: string, en: string, ja?: string) =>
    lang === "zh" ? zh : lang === "ja" ? (ja ?? en) : en

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
        {L("统计面板已停用", "Stats dashboard retired", "統計ダッシュボードは廃止されました")}
      </h1>

      <p style={{ marginBottom: "1.5rem" }}>
        {L(
          "此面板依赖心跳 telemetry 接口。该接口连同全部数据收集通道已被移除，因此不再有可展示的数据。",
          "This dashboard was backed by the heartbeat telemetry endpoint. That endpoint was removed along with every other data-collection channel, so there is nothing left to display.",
          "このダッシュボードはハートビート計測エンドポイントに依存していました。当該エンドポイントは他のデータ収集チャネルとともに削除されたため、表示できるデータはありません。"
        )}
      </p>

      <p>
        <a href="/">{L("← 返回首页", "← Back to home", "← ホームに戻る")}</a>
      </p>
    </main>
  )
}
