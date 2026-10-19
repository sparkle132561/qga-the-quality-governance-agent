import { ref } from 'vue'

export type Actor = { user_id: string; role: 'merchant' | 'reviewer' | 'admin'; merchant_id: string | null }
export type Case = { id: string; title: string; merchant_id: string; workflow: string; status: string; revision: number; is_demo: boolean; payload: Record<string, unknown>; latest_analysis: Analysis | null }
export type Impact={affected_product_ids:string[];excluded_product_ids:string[];unknown_product_ids:string[];affected_order_ids:string[];findings:{product_id:string;batch_id:string|null;status:string;reasons:string[];production_date?:string|null;model?:string;order_ids:string[];graph_edges?:{subject:string;predicate:string;object:string;provenance:Record<string,unknown>}[]}[]}
export type Analysis = {run_id:string;thread_id:string;revision:number;impact?:Impact|null;engine_mode: string; elapsed_ms: number | null; recommendation: string; missing_fields?:string[];conflicts?:Record<string,unknown>[];branches: Record<string, { status: string; message: string; evidence: {id:string;text:string;location:Record<string,unknown>}[];trace?:Record<string,unknown>[];error_code?:string;engine_mode?:string }> }
export type Audit = { id: string; action: string; actor_id: string; created_at: string; details: Record<string, unknown> }
export const actor = ref<Actor | null>(null)
let token = sessionStorage.getItem('quality-token') || ''
let generation = 0
export const error = ref('')

export class StaleRequestError extends Error {}
export class ApiError extends Error {
  constructor(public status:number,public detail:unknown,message:string){super(message)}
}
export function reportError(cause: unknown, fallback: string) {
  if (!(cause instanceof StaleRequestError)) error.value = cause instanceof Error ? cause.message : fallback
}

export function logout() {
  generation++
  token = ''
  sessionStorage.removeItem('quality-token')
  actor.value = null
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const requestGeneration = generation
  const headers = new Headers(options.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  let response: Response
  try { response = await fetch(`/api${path}`, { ...options, headers }) }
  catch {
    if (requestGeneration !== generation) throw new StaleRequestError()
    throw new Error('服务暂时不可达，请检查后端是否启动。')
  }
  const payload = await response.json().catch(() => null)
  if (requestGeneration !== generation) throw new StaleRequestError()
  if (!response.ok) {
    if (response.status === 401) logout()
    throw new ApiError(response.status,payload?.detail,typeof payload?.detail === 'string' ? payload.detail :
      typeof payload?.detail?.message === 'string' ? payload.detail.message : `请求失败（${response.status}）`)
  }
  return payload as T
}

export async function signIn(username: string) {
  const result = await api<{ access_token: string; actor: Actor }>('/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password: 'demo-only' }),
  })
  token = result.access_token
  generation++
  sessionStorage.setItem('quality-token', token)
  actor.value = result.actor
}

export async function imageObjectUrl(path: string): Promise<string> {
  const requestGeneration=generation
  const response=await fetch(`/api${path}`,{headers:{Authorization:`Bearer ${token}`},cache:'no-store'})
  if(requestGeneration!==generation)throw new StaleRequestError()
  if(!response.ok)throw new Error(`图片预览读取失败（${response.status}）`)
  const blob=await response.blob()
  if(requestGeneration!==generation)throw new StaleRequestError()
  return URL.createObjectURL(blob)
}

export async function downloadFile(path: string, filename: string) {
  const requestGeneration = generation
  const response = await fetch(`/api${path}`, {headers:{Authorization:`Bearer ${token}`}})
  if (requestGeneration !== generation) throw new StaleRequestError()
  if (!response.ok) {
    if (response.status === 401) logout()
    throw new Error(`原始文件下载失败（${response.status}）`)
  }
  const blob = await response.blob()
  if (requestGeneration !== generation) throw new StaleRequestError()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url; link.download = filename; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function restore() {
  if (!token) return
  try { actor.value = await api<Actor>('/auth/me') }
  catch (cause) { if (!(cause instanceof StaleRequestError)) logout() }
}

export const workflowLabels: Record<string, string> = {
  qualification: '准入审核', incident: '质量事件', remediation: '整改与申诉',
}
export const statusLabels: Record<string, string> = {
  draft: '草稿', submitted: '已提交', analyzing: '分析中', waiting_review: '待审核',
  needs_info: '待补充', approved: '已通过', confirmed: '已确认', rejected: '已拒绝',
}
export const roleLabels: Record<string, string> = { merchant: '商家', reviewer: '审核员', admin: '管理员' }
