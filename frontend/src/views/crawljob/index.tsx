import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/crawljob'
const columns = [
  "作业编号",
  "目标站点",
  "起始URL",
  "爬取深度",
  "URL去重策略",
  "页面解析器",
  "下载超时秒",
  "最大页面数",
  "过滤规则快照",
  "执行参数URL数",
  "拦截URL数"
]
const actions = [
  "提交作业审批",
  "启动作业",
  "停止作业"
]
const statuses = [
  "草稿",
  "作业审批中",
  "执行中",
  "已完成",
  "已停止"
]
const stats = [
  {
    "label": "作业定义总数",
    "value": 0
  },
  {
    "label": "执行中作业",
    "value": 0
  },
  {
    "label": "已完成作业",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function CrawljobView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const params = (detail?.['执行参数'] ?? null) as Record<string, unknown> | null
  const asItem = (value: unknown) => value as Record<string, unknown>
  const asList = (value: unknown) => (Array.isArray(value) ? (value as Record<string, unknown>[]) : [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('作业定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '抓取作业列表读取失败')
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
      const payload = await response.json().catch(() => ({ ok: false, message: '抓取作业动作未生效' }))
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? '抓取作业动作未生效，请稍后重试')
        return
      }
      setNotice(payload.message ?? '')
      if (payload.entry) setDetail(payload.entry)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '抓取作业操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('作业明细读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '作业明细读取失败')
    }
  }

  return (
    <section className="page" data-module="crawljob">
      <header className="page-head">
        <div>
          <h2>抓取作业管理</h2>
          <p className="page-desc">定义单次抓取作业的范围和参数，包括深度、去重和资源上限。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('作业定义登记入口尚未接入审批流')}>
            登记作业定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出抓取作业清单
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
                <button className="link" type="button" onClick={() => void openDetail(row)}>查看执行参数</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无抓取作业数据，可先登记作业定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="detail-card">
          <header className="detail-head">
            <h3>作业执行参数 · {String(detail['作业编号'] ?? '')}</h3>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>
          <p className="page-desc">
            过滤规则快照 {String(detail['过滤规则快照'] ?? '—')}；
            执行参数快照 {String(detail['执行参数快照'] ?? '—')}；
            准入URL {String(detail['执行参数URL数'] ?? 0)} 条，拦截 {String(detail['拦截URL数'] ?? 0)} 条。
          </p>
          {params ? (
            <>
              <div className="summary-row">
                <span className="summary-item">规则编号：{String(params['规则编号'] ?? '—')}</span>
                <span className="summary-item">规则版本：{String(params['规则版本'] ?? '—')}</span>
                <span className="summary-item">快照校验和：{String(params['快照校验和'] ?? '—')}</span>
                <span className="summary-item">冲突策略：{String(params['冲突策略'] ?? '—')}</span>
              </div>
              <h4>准入URL清单（进入执行参数）</h4>
              <ul className="plain-list">
                {asList(params['准入明细']).map((item, index) => (
                  <li key={index}>{String(asItem(item)['规范化URL'])} — {String(asItem(item)['命中说明'])}</li>
                ))}
              </ul>
              <h4>拦截URL清单（不进入执行参数）</h4>
              <ul className="plain-list">
                {asList(params['拦截URL清单']).map((item, index) => (
                  <li key={index}>{String(asItem(item)['规范化URL'])} — {String(asItem(item)['原因'])}</li>
                ))}
              </ul>
            </>
          ) : (
            <p className="page-desc">该作业尚未启动作业，没有生成执行参数。</p>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条抓取作业记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
