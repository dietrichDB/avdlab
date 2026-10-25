import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './hooks/useAuth'
import { JobDrawerProvider } from './components/JobDrawer'
import { Layout } from './components/Layout'
import { Spinner } from './components/ui'
import LoginPage, { ForcedPasswordChange } from './pages/Login'
import AccountPage from './pages/Account'
import UsersPage from './pages/Users'
import RegisterPage from './pages/Register'
import AdminSettingsPage from './pages/AdminSettings'
import DashboardPage from './pages/Dashboard'
import HostsPage from './pages/Hosts'
import DevcontainerPage from './pages/Devcontainer'
import ImagesPage from './pages/Images'
import LabsPage from './pages/Labs'
import NewLabPage from './pages/NewLab'
import LabDetailPage from './pages/lab/LabDetail'
import JobsPage from './pages/Jobs'
import JobPage from './pages/Job'
import CloudVisionPage from './pages/CloudVision'
import HostDetailPage from './pages/HostDetail'
import NetBoxPage from './pages/NetBox'
import ChangelogPage from './pages/Changelog'
import AboutPage from './pages/About'
import FeedbackAdminPage from './pages/FeedbackAdmin'
import LearnPage from './pages/Learn'
import GettingStartedPage from './pages/GettingStarted'
import LessonPage from './pages/Lesson'
import { TourProvider } from './components/learn/Tour'

function RequireAuth({ children }: { children: JSX.Element }) {
  const { user, loading, mustChangePassword } = useAuth()
  const loc = useLocation()
  if (loading) return <Spinner label="Checking session…" />
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  if (mustChangePassword) return <ForcedPasswordChange />
  return children
}

export default function App() {
  return (
    <AuthProvider>
      <JobDrawerProvider>
        <TourProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route element={<RequireAuth><Layout /></RequireAuth>}>
            <Route index element={<DashboardPage />} />
            <Route path="hosts" element={<HostsPage />} />
            <Route path="hosts/:id" element={<HostDetailPage />} />
            <Route path="devcontainer" element={<DevcontainerPage />} />
            <Route path="images" element={<ImagesPage />} />
            <Route path="labs" element={<LabsPage />} />
            <Route path="labs/new" element={<NewLabPage />} />
            <Route path="labs/:id" element={<LabDetailPage />} />
            <Route path="labs/:id/:tab" element={<LabDetailPage />} />
            <Route path="cloudvision" element={<CloudVisionPage />} />
            <Route path="netbox" element={<NetBoxPage />} />
            <Route path="jobs" element={<JobsPage />} />
            <Route path="jobs/:id" element={<JobPage />} />
            <Route path="changelog" element={<ChangelogPage />} />
            <Route path="about" element={<AboutPage />} />
            <Route path="feedback" element={<FeedbackAdminPage />} />
            <Route path="account" element={<AccountPage />} />
            <Route path="users" element={<UsersPage />} />
            <Route path="admin/settings" element={<AdminSettingsPage />} />
            <Route path="learn" element={<LearnPage />} />
            <Route path="getting-started" element={<GettingStartedPage />} />
            <Route path="learn/lessons/:id" element={<LessonPage />} />
            <Route path="learn/:section" element={<LearnPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
        </TourProvider>
      </JobDrawerProvider>
    </AuthProvider>
  )
}
