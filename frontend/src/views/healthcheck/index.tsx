import { useMemo, useState } from 'react'

import { useModuleRows } from '@/api/useModuleRows'

const ENDPOINT = '/api/healthcheck'
const columns = [
  '报告编号',
  '采集器名称',
  'CPU占用率',
  'CPU峰值',
  '错误队列长度',
  '采样窗口开始',
  '采样窗口结束',
  '检查时间戳'
]

// 健康报告状态机：草稿→报告中→待确认→已确认→已归档，只暴露合法的下一步动作。
const NEXT_ACTIONS: Record<string, string[]> = {
  草稿: ['生成报告'],
  报告中: ['确认报告', '归档报告'],
  待确认: ['确认报告', '归档报告'],
  已确认: ['归档报告'],
  已归档: []
}

type Row = Record<string, string | number | null>

export default function HealthcheckView() {
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => ['报告编号', '采集器名称', 'CPU占用率'], [])

  const { rows, total, errorMessage, infoMessage, reload, runAction } = useModuleRows<Row>({
    endpoint: ENDPOINT,
    filters,
    listErrorMessage: '采集器健康列表读取失败',
  })

  async function handleAction(action: string, row: Row) {
    await runAction(
      `${ENDPOINT}/${row.id}/actions`,
      { values: { action } },
      '采集器健康动作未生效，请稍后重试',
    )
  }

  return (
    <section className="page" data-module="healthcheck">
      <header className="page-head">
        <div>
          <h2>采集器健康管理</h2>
          <p className="page-desc">
            报告按 草稿→报告中→待确认→已确认→已归档 单向推进；确认前必须齐备采样窗口、CPU峰值与错误队列长度，
            已确认报告窗口过期后自动退回待确认。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn primary"
            type="button"
            onClick={() => window.alert('健康报告登记入口尚未接入审批流')}
          >
            登记健康报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集器健康清单
          </button>
        </div>
      </header>

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
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const rowStatus = String(row.status ?? '')
            const allowed = NEXT_ACTIONS[rowStatus] ?? []
            return (
              <tr key={String(row.id)}>
                <td><span className={`status-tag status-${rowStatus}`}>{rowStatus}</span></td>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td className="row-actions">
                  {allowed.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void handleAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  {allowed.length === 0 && <span className="muted">已归档，无可用动作</span>}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无采集器健康数据，可先登记健康报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条采集器健康记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
