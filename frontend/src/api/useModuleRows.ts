import { useCallback, useEffect, useRef, useState } from 'react'

import { request } from '@/api/client'

type PagePayload<T> = { items: T[]; total: number }

/**
 * 列表页统一数据加载钩子。
 *
 * 修复两类线上问题：
 * 1. 导航页/详情页结果错位、重复显示、返回后不更新 —— 旧请求晚于新请求返回时会把
 *    过期数据写回页面。这里用自增序号只接受最后一次请求的响应，并在卸载时作废。
 * 2. 动作失败只显示固定文案，后端的拦截原因（未确认报告、状态回退、重复恢复…）
 *    看不到 —— 动作方法统一解析 ActionResult.message 原样透出。
 */
export function useModuleRows<T extends Record<string, unknown>>({
  endpoint,
  filters,
  listErrorMessage,
}: {
  endpoint: string
  filters: Record<string, string>
  listErrorMessage: string
}) {
  const [rows, setRows] = useState<T[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const seqRef = useRef(0)

  const reload = useCallback(async () => {
    const seq = ++seqRef.current
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${endpoint}?${query}`)
      if (!response.ok) {
        throw new Error(listErrorMessage)
      }
      const payload = (await response.json()) as PagePayload<T>
      // 只接受最新一次请求的结果，迟到的旧响应直接丢弃，杜绝结果错位。
      if (seq !== seqRef.current) return
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      if (seq !== seqRef.current) return
      setErrorMessage(error instanceof Error ? error.message : listErrorMessage)
    }
  }, [endpoint, filters, listErrorMessage])

  useEffect(() => {
    void reload()
    return () => {
      // 离开页面（导航返回/切换模块）时作废在途请求，避免旧数据覆盖新页面。
      seqRef.current += 1
    }
  }, [reload])

  /** 执行动作并把后端的 ok/message 原样透出，保证拦截原因可见、列表强制刷新。 */
  const runAction = useCallback(
    async (path: string, body: Record<string, unknown>, fallbackMessage: string) => {
      const seq = ++seqRef.current
      setErrorMessage('')
      setInfoMessage('')
      try {
        const response = await request(path, {
          method: 'POST',
          body: JSON.stringify(body),
        })
        const payload = (await response.json().catch(() => null)) as
          | { ok?: boolean; message?: string }
          | null
        if (!response.ok || !payload) {
          throw new Error(payload?.message ?? fallbackMessage)
        }
        if (payload.ok === false) {
          // 业务拦截（状态机/健康联动）：HTTP 200 但 ok=false，必须把原因显示出来。
          setErrorMessage(payload.message ?? fallbackMessage)
        } else {
          setInfoMessage(payload.message ?? '操作已生效')
        }
      } catch (error) {
        if (seq !== seqRef.current) return false
        setErrorMessage(error instanceof Error ? error.message : fallbackMessage)
      }
      await reload()
      return seq === seqRef.current
    },
    [reload],
  )

  return { rows, total, errorMessage, infoMessage, reload, runAction, setErrorMessage, setInfoMessage }
}
