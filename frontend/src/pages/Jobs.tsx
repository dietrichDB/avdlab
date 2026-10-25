import { useQuery } from '@tanstack/react-query'
import { jobsApi } from '../api/jobs'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, Spinner, ErrorBox } from '../components/ui'

export default function JobsPage() {
  const jobs = useQuery({ queryKey: ['jobs', { limit: 100 }], queryFn: () => jobsApi.list({ limit: 100 }), refetchInterval: 5000 })
  return (
    <div>
      <PageHeader title="Jobs" sub="All jobs, newest first" />
      <ErrorBox error={jobs.error} />
      {jobs.isLoading ? <Spinner /> : <JobsTable jobs={jobs.data ?? []} />}
    </div>
  )
}
