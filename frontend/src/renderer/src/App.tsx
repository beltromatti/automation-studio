import { Routes, Route, Navigate } from "react-router-dom";
import { RunsProvider } from "@/components/RunsProvider";
import { Sidebar } from "@/components/Sidebar";
import Overview from "@/pages/Overview";
import WorkflowPage from "@/pages/WorkflowPage";
import RunsPage from "@/pages/RunsPage";
import RunDetail from "@/pages/RunDetail";
import ProfilesPage from "@/pages/ProfilesPage";
import DatasetsPage from "@/pages/DatasetsPage";
import DatasetPage from "@/pages/DatasetPage";
import FilesPage from "@/pages/FilesPage";
import AgentsPage from "@/pages/AgentsPage";
import AgentLaunchPage from "@/pages/AgentLaunchPage";
import AgentSessionsPage from "@/pages/AgentSessionsPage";
import AgentSessionPage from "@/pages/AgentSessionPage";

export default function App() {
  return (
    <RunsProvider>
      <div className="flex h-screen bg-bg">
        <Sidebar />
        {/* Linear-style floating panel: the sidebar sits on the black app background,
            and the content "rests" on top of it — inset on every side, rounded, bordered.
            This wrapper supplies the margins; <main> stays the scroll container with a
            definite height so pages relying on h-full keep working unchanged. */}
        <div className="flex-1 min-w-0 p-2 pt-[48px]">
          <main className="app-panel h-full overflow-y-auto">
          <Routes>
            <Route path="/" element={<Navigate to="/agents" replace />} />
            <Route path="/workflows" element={<Overview />} />
            <Route path="/workflows/:id" element={<WorkflowPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/agents/sessions" element={<AgentSessionsPage />} />
            <Route path="/agents/sessions/:id" element={<AgentSessionPage />} />
            <Route path="/agents/:id" element={<AgentLaunchPage />} />
            <Route path="/data" element={<DatasetsPage />} />
            <Route path="/data/:id" element={<DatasetPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/profiles" element={<ProfilesPage />} />
          </Routes>
          </main>
        </div>
      </div>
    </RunsProvider>
  );
}
