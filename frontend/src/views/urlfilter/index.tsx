import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/urlfilter'
const columns = [
  "规则编号",
  "包含模式",
  "排除模式",
  "域名白名单",
  "路径黑名单",
  "参数剥离规则",
  "大小写处理",
  "尾部斜杠策略"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]
const statuses = [
  "草稿",
  "规则评审中",
  "已生效",
  "已废弃"
]

/** 登记/编辑表单覆盖的完整字段；字段名与后端规则字段一一对应。 */
const formFields: { key: string; label: string; hint: string; multiline?: boolean }[] = [
  { key: '规则编号', label: '规则编号', hint: '唯一，最长 64 字符' },
  { key: '包含模式', label: '包含模式', hint: '正则，每行一条', multiline: true },
  { key: '排除模式', label: '排除模式', hint: '正则，每行一条', multiline: true },
  { key: '域名白名单', label: '域名白名单', hint: '逗号分隔，支持 *.example.com' },
  { key: '路径黑名单', label: '路径黑名单', hint: '以 / 开头，支持 * 与 re: 正则', multiline: true },
  { key: '参数剥离规则', label: '参数剥离规则', hint: '参数名，逗号分隔' },
  { key: '大小写处理', label: '大小写处理', hint: '域名小写 / 全部小写 / 保留原样' },
  { key: '尾部斜杠策略', label: '尾部斜杠策略', hint: '保留 / 去除 / 统一添加' },
  { key: '匹配范围', label: '匹配范围', hint: '全站 / 域名 / 路径' },
  { key: '规则上限', label: '规则上限', hint: '候选URL上限，1-2000' },
  { key: '冲突策略', label: '冲突策略', hint: '当前固定为拒绝优先' },
  { key: '候选URL', label: '候选URL', hint: '每行一个，用于生成去重URL清单', multiline: true },
  { key: '备注', label: '备注', hint: '可空' },
]

type Row = Record<string, string | number | null>

type FormValues = Record<string, string>

type Errors = { field: string; reason: string; index?: number; value?: string }[]

type Snapshot = {
  id: number
  '规则id'?: number
  规则编号?: string
  规则版本?: number
  统计?: Record<string, unknown>
  生成时间?: string
  同步状态?: string
  快照校验和?: string
  规则快照摘要?: Record<string, string>
}

const emptyForm = (): FormValues => {
  const values: FormValues = {}
  for (const field of formFields) values[field.key] = ''
  values['大小写处理'] = '域名小写'
  values['尾部斜杠策略'] = '去除'
  values['匹配范围'] = '全站'
  values['规则上限'] = '1000'
  values['冲突策略'] = '拒绝优先'
  return values
}

function text(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.join('\n')
  return String(value)
}

export default function UrlfilterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<Row | null>(null)
  const [form, setForm] = useState<FormValues>(emptyForm)
  const [formErrors, setFormErrors] = useState<Errors>([])
  const [activeSnapshot, setActiveSnapshot] = useState<Snapshot | null>(null)
  const [snapshotUrls, setSnapshotUrls] = useState<Row[]>([])
  const [snapshotFilter, setSnapshotFilter] = useState<'全部' | '准入' | '拒绝'>('全部')
  const [syncRecords, setSyncRecords] = useState<Row[]>([])
  const [policy, setPolicy] = useState<Record<string, unknown> | null>(null)
  const [showPolicy, setShowPolicy] = useState(false)
  const filterFields = useMemo(() => ['规则编号'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('过滤规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'URL过滤列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    request(`${ENDPOINT}/policy`)
      .then((response) => response.json())
      .then((payload) => setPolicy(payload))
      .catch(() => undefined)
  }, [])

  const loadSnapshot = useCallback(async (snapshotId: number, decision: '全部' | '准入' | '拒绝') => {
    setActiveSnapshot((prev) => (prev?.id === snapshotId ? prev : { id: snapshotId }))
    try {
      const query = decision === '全部' ? '' : `?decision=${decision}`
      const response = await request(`${ENDPOINT}/snapshots/${snapshotId}/urls${query}`)
      if (!response.ok) throw new Error('快照URL清单读取失败')
      const payload = await response.json()
      setSnapshotUrls(payload.items ?? [])
      const meta = await request(`${ENDPOINT}/snapshots/${snapshotId}`)
      if (meta.ok) {
        const detail = await meta.json()
        setActiveSnapshot(detail)
      }
      const records = await request(`${ENDPOINT}/sync-records?snapshot_id=${snapshotId}`)
      if (records.ok) {
        const recordPayload = await records.json()
        setSyncRecords(recordPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照读取失败')
    }
  }, [])

  function openCreate() {
    setEditing(null)
    setForm(emptyForm())
    setFormErrors([])
    setShowForm(true)
    setNotice('')
  }

  function openEdit(row: Row) {
    const values = emptyForm()
    for (const field of formFields) values[field.key] = text(row[field.key])
    setForm(values)
    setEditing(row)
    setFormErrors([])
    setShowForm(true)
    setNotice('')
  }

  async function saveRule() {
    setErrorMessage('')
    setFormErrors([])
    const values: Record<string, unknown> = { ...form }
    if (editing) {
      values['version'] = editing['version']
    }
    const path = editing ? `${ENDPOINT}/${editing.id}` : ENDPOINT
    try {
      const response = await request(path, {
        method: editing ? 'PUT' : 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        setFormErrors(payload.errors ?? [])
        setErrorMessage(payload.message ?? '过滤规则未保存')
        return
      }
      setShowForm(false)
      setNotice(payload.message ?? '过滤规则已保存')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '过滤规则保存失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: 'URL过滤动作未生效' }))
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? 'URL过滤动作未生效，请稍后重试')
        return
      }
      setNotice(payload.message ?? '')
      await reload()
      const snapshotId = payload.entry?.['当前快照']
      if (snapshotId) await loadSnapshot(Number(snapshotId), snapshotFilter)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'URL过滤操作失败')
    }
  }

  async function verifySnapshot() {
    if (!activeSnapshot) return
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/snapshots/${activeSnapshot.id}/verify`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message ?? '快照复核失败')
      if (payload.ok) setNotice(payload.message)
      else setErrorMessage(payload.message)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照复核失败')
    }
  }

  async function syncSnapshot() {
    if (!activeSnapshot) return
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/snapshots/${activeSnapshot.id}/sync`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? '同步失败')
        return
      }
      setNotice(payload.message ?? '')
      await loadSnapshot(activeSnapshot.id, snapshotFilter)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '同步失败')
    }
  }

  function changeSnapshotFilter(decision: '全部' | '准入' | '拒绝') {
    setSnapshotFilter(decision)
    if (activeSnapshot) void loadSnapshot(activeSnapshot.id, decision)
  }

  return (
    <section className="page" data-module="urlfilter">
      <header className="page-head">
        <div>
          <h2>URL过滤管理</h2>
          <p className="page-desc">管理URL抓取范围的过滤规则，支持正则包含排除和清单控制。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={openCreate}>登记过滤规则</button>
          <button className="btn" type="button" onClick={() => setShowPolicy((prev) => !prev)}>查看规则口径</button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出URL过滤清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">过滤规则总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已生效规则</span>
          <strong className="stat-value">{rows.filter((row) => row.status === '已生效').length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">评审中规则</span>
          <strong className="stat-value">{rows.filter((row) => row.status === '规则评审中').length}</strong>
        </article>
      </div>

      {showPolicy && policy && (
        <section className="detail-card">
          <h3>规则口径</h3>
          <div className="policy-grid">
            {Object.entries(policy).map(([key, value]) => (
              <article key={key} className="policy-item">
                <span className="stat-label">{key}</span>
                <div className="policy-value">
                  {typeof value === 'string'
                    ? value
                    : Object.entries(value as Record<string, unknown>).map(([subKey, subValue]) => (
                      <span key={subKey} className="policy-line">
                        <b>{subKey}</b>：
                        {Array.isArray(subValue) ? subValue.join(' / ') : String(subValue)}
                      </span>
                    ))}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {showForm && (
        <section className="detail-card">
          <h3>{editing ? `编辑过滤规则 ${editing['规则编号']}` : '登记过滤规则'}</h3>
          {editing && (
            <p className="page-desc">
              当前版本 {String(editing.version)}，整条规则一次覆盖保存；保存后已生效规则会回到规则评审中，旧快照保留。
            </p>
          )}
          <form className="rule-form" onSubmit={(event) => { event.preventDefault(); void saveRule() }}>
            {formFields.map((field) => (
              <label key={field.key} className="filter-item">
                <span>{field.label}</span>
                {field.multiline ? (
                  <textarea
                    rows={3}
                    value={form[field.key] ?? ''}
                    placeholder={field.hint}
                    onChange={(event) => setForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                  />
                ) : (
                  <input
                    value={form[field.key] ?? ''}
                    placeholder={field.hint}
                    onChange={(event) => setForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                  />
                )}
              </label>
            ))}
            {formErrors.length > 0 && (
              <div className="field-errors">
                {formErrors.map((item, index) => (
                  <span key={`${item.field}-${index}`} className="error-text">
                    {item.field}{item.index ? ` 第 ${item.index} 项` : ''}：{item.reason}
                  </span>
                ))}
              </div>
            )}
            <div className="form-actions">
              <button className="btn primary" type="submit">{editing ? '保存规则' : '登记规则'}</button>
              <button className="btn ghost" type="button" onClick={() => setShowForm(false)}>取消</button>
            </div>
          </form>
        </section>
      )}

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
            <th>状态</th>
            <th>版本</th>
            <th>当前快照</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status ?? '—'}</td>
              <td>{row.version ?? '—'}</td>
              <td>
                {row['当前快照'] ? (
                  <button className="link" type="button" onClick={() => void loadSnapshot(Number(row['当前快照']), snapshotFilter)}>
                    快照 {String(row['当前快照'])}（版本 {String(row['当前快照版本'])}）
                  </button>
                ) : '—'}
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => openEdit(row)}>编辑规则</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无URL过滤数据，可先登记过滤规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {activeSnapshot && (
        <section className="detail-card" data-testid="snapshot-panel">
          <header className="detail-head">
            <h3>过滤规则快照 {activeSnapshot.id}</h3>
            <div className="detail-actions">
              <button className="btn" type="button" onClick={() => void verifySnapshot()}>逐项复核</button>
              <button className="btn" type="button" onClick={() => void syncSnapshot()}>同步到页面落地侧</button>
              <button className="btn ghost" type="button" onClick={() => { setActiveSnapshot(null); setSnapshotUrls([]); setSyncRecords([]) }}>
                关闭
              </button>
            </div>
          </header>
          <p className="page-desc">
            规则 {activeSnapshot['规则编号'] ?? '—'} 版本 {String(activeSnapshot['规则版本'] ?? '—')}；
            生成时间 {activeSnapshot['生成时间'] ?? '—'}；同步状态 {activeSnapshot['同步状态'] ?? '—'}。
          </p>
          {activeSnapshot.统计 && (
            <div className="summary-row">
              {Object.entries(activeSnapshot.统计).map(([key, value]) => (
                <span key={key} className="summary-item">
                  {key}：{typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}
                </span>
              ))}
            </div>
          )}
          {activeSnapshot.规则快照摘要 && (
            <div className="summary-row">
              {Object.entries(activeSnapshot.规则快照摘要).map(([key, value]) => (
                <span key={key} className="summary-item">{key}：{value || '—'}</span>
              ))}
            </div>
          )}
          <div className="filter-bar">
            {(['全部', '准入', '拒绝'] as const).map((decision) => (
              <button
                key={decision}
                className={snapshotFilter === decision ? 'btn primary' : 'btn'}
                type="button"
                onClick={() => changeSnapshotFilter(decision)}
              >
                {decision}
              </button>
            ))}
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>序号</th>
                <th>原始URL</th>
                <th>规范化URL</th>
                <th>决定</th>
                <th>命中字段</th>
                <th>命中值</th>
                <th>冲突</th>
                <th>重复次数</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {snapshotUrls.map((row) => (
                <tr key={String(row.id)}>
                  <td>{row['序号'] ?? '—'}</td>
                  <td className="url-cell">{row['原始URL'] ?? '—'}</td>
                  <td className="url-cell">{row['规范化URL'] ?? '—'}</td>
                  <td>{row['决定'] ?? '—'}</td>
                  <td>{row['命中字段'] ?? '—'}</td>
                  <td className="url-cell">{row['命中值'] ?? '—'}</td>
                  <td>{row['冲突'] ? '是' : '否'}</td>
                  <td>{row['重复次数'] ?? '—'}</td>
                  <td>{row['原因'] ?? '—'}</td>
                </tr>
              ))}
              {snapshotUrls.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>该快照没有符合条件的URL</td></tr>
              )}
            </tbody>
          </table>
          {syncRecords.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>同步时间</th>
                  <th>目标清单</th>
                  <th>新增</th>
                  <th>已存在</th>
                  <th>拦截</th>
                  <th>同步结果</th>
                </tr>
              </thead>
              <tbody>
                {syncRecords.map((row) => (
                  <tr key={String(row.id)}>
                    <td>{row['同步时间'] ?? '—'}</td>
                    <td>{row['目标清单'] ?? '—'}</td>
                    <td>{row['新增数'] ?? '—'}</td>
                    <td>{row['已存在数'] ?? '—'}</td>
                    <td>{row['拦截数'] ?? '—'}</td>
                    <td>{row['同步结果'] ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条URL过滤记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
