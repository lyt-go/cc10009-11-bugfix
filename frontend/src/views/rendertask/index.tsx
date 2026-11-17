import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rendertask'
const columns = [
  "作业编号",
  "目标URL",
  "浏览器版本",
  "视口宽度",
  "视口高度",
  "等待策略",
  "脚本注入",
  "超时阈值秒",
  "过滤规则快照",
  "规则编号",
  "准入结果"
]
const actions = [
  "提交排队",
  "启动渲染",
  "终止渲染"
]
const statuses = [
  "草稿",
  "排队中",
  "渲染中",
  "已完成",
  "已超时"
]
const stats = [
  {
    "label": "渲染作业总数",
    "value": 0
  },
  {
    "label": "渲染中作业",
    "value": 0
  },
  {
    "label": "已完成作业",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function RendertaskView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [snapshotId, setSnapshotId] = useState('')
  const [snapshotView, setSnapshotView] = useState<Record<string, unknown> | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('渲染作业列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '渲染任务动作未生效' }))
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? '渲染任务动作未生效，请稍后重试')
        return
      }
      setNotice(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务操作失败')
    }
  }

  async function loadSnapshotView() {
    setErrorMessage('')
    if (!snapshotId.trim()) {
      setErrorMessage('请先填写过滤规则快照编号')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/snapshots/${snapshotId.trim()}`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? '页面落地侧清单读取失败')
      setSnapshotView(payload)
    } catch (error) {
      setSnapshotView(null)
      setErrorMessage(error instanceof Error ? error.message : '页面落地侧清单读取失败')
    }
  }

  const landingItems = Array.isArray(snapshotView?.['落地清单'])
    ? (snapshotView?.['落地清单'] as Record<string, unknown>[])
    : []

  return (
    <section className="page" data-module="rendertask">
      <header className="page-head">
        <div>
          <h2>渲染任务管理</h2>
          <p className="page-desc">管理需要浏览器渲染的页面抓取作业，配置渲染环境和等待策略。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('渲染作业登记入口尚未接入审批流')}>
            登记渲染作业
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出渲染任务清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无渲染任务数据，可先登记渲染作业</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="detail-card">
        <header className="detail-head">
          <h3>页面落地侧清单核对</h3>
          <div className="detail-actions">
            <input
              className="inline-input"
              value={snapshotId}
              placeholder="过滤规则快照编号"
              onChange={(event) => setSnapshotId(event.target.value)}
            />
            <button className="btn" type="button" onClick={() => void loadSnapshotView()}>按快照核对</button>
          </div>
        </header>
        {snapshotView ? (
          <>
            <div className="summary-row">
              <span className="summary-item">规则编号：{String(snapshotView['规则编号'] ?? '—')}</span>
              <span className="summary-item">规则版本：{String(snapshotView['规则版本'] ?? '—')}</span>
              <span className="summary-item">落地条目数：{String(snapshotView['落地条目数'] ?? 0)}</span>
              <span className="summary-item">快照校验和：{String(snapshotView['快照校验和'] ?? '—')}</span>
              <span className="summary-item">快照统计：{JSON.stringify(snapshotView['统计'] ?? {})}</span>
            </div>
            <table className="data-table">
              <thead>
                <tr><th>落地编号</th><th>目标URL</th><th>原始URL</th><th>来源快照</th><th>准入结果</th><th>同步时间</th></tr>
              </thead>
              <tbody>
                {landingItems.map((item) => (
                  <tr key={String(item.id)}>
                    <td>{String(item['作业编号'] ?? '—')}</td>
                    <td className="url-cell">{String(item['目标URL'] ?? '—')}</td>
                    <td className="url-cell">{String(item['原始URL'] ?? '—')}</td>
                    <td>{String(item['过滤规则快照'] ?? '—')}</td>
                    <td>{String(item['准入结果'] ?? '—')}</td>
                    <td>{String(item['同步时间'] ?? '—')}</td>
                  </tr>
                ))}
                {landingItems.length === 0 && (
                  <tr><td className="empty-state" colSpan={6}>该快照没有同步到页面落地侧条目</td></tr>
                )}
              </tbody>
            </table>
          </>
        ) : (
          <p className="page-desc">填写过滤规则快照编号后核对页面落地侧清单；违规URL不会出现在清单中。</p>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条渲染任务记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
