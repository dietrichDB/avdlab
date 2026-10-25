import { api } from './client'
import type {
  AiApplyIn, AiApplyOut, AiDesignIn, AiDesignOut, AiLinkIn, AiLinkView, AiProbeResult, AiSharedSettings, AiSharedSettingsIn,
} from './types'

/** v0.10.0: AI lab designer (bring your own model) */
export const aiApi = {
  link: () => api.get<AiLinkView>('/ai/link'),
  saveLink: (body: AiLinkIn) => api.put<AiLinkView>('/ai/link', body),
  removeLink: () => api.delete<void>('/ai/link'),
  /** probe the unsaved form values (the stored key is used when api_key is empty) or the saved link */
  testLink: (body: Partial<AiLinkIn> = {}) => api.post<AiProbeResult>('/ai/link/test', body),
  design: (body: AiDesignIn) => api.post<AiDesignOut>('/ai/design', body),
  apply: (body: AiApplyIn) => api.post<AiApplyOut>('/ai/design/apply', body),
  shared: () => api.get<AiSharedSettings>('/admin/ai'),
  saveShared: (body: AiSharedSettingsIn) => api.put<AiSharedSettings>('/admin/ai', body),
  testShared: () => api.post<AiProbeResult>('/admin/ai/test'),
}

export const AI_PROVIDERS: { id: 'anthropic' | 'openai'; label: string; placeholder: string; modelHint: string }[] = [
  { id: 'anthropic', label: 'Anthropic (Claude)', placeholder: 'https://api.anthropic.com', modelHint: 'exact model id from the Anthropic console, e.g. claude-sonnet-4-5' },
  { id: 'openai', label: 'OpenAI-compatible (OpenAI, Ollama, LM Studio, vLLM, Azure)', placeholder: 'https://api.openai.com/v1 · http://host:11434/v1', modelHint: 'e.g. gpt-4o, llama3.1, qwen2.5' },
]
