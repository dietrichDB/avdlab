import { useQuery } from '@tanstack/react-query'
import { jobsApi } from '../../api/jobs'
import type { Lab } from '../../api/types'
import { JobsTable } from '../../components/JobsTable'
import { ErrorBox, Spinner } from '../../components/ui'

export default function LabJobsTab({ lab }: { lab: Lab }) {
  const jobs = useQuery({ queryKey: ['jobs', { lab_id: lab.id }], queryFn: () => jobsApi.list({ lab_id: lab.id }), refetchInterval: 5000 })
  if (jobs.isLoading) return <Spinner />
  return <><ErrorBox error={jobs.error} /><JobsTable jobs={jobs.data ?? []} showTarget={false} /></>
}
