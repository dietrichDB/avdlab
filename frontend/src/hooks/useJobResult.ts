import { useQuery } from '@tanstack/react-query'
import { isTerminal, jobsApi } from '../api/jobs'

/** Poll a job (2 s) until it reaches a terminal state. */
export function useJobResult(jobId: string | null) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : 2000),
  })
}
