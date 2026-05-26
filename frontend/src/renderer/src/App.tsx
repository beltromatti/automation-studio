import { Routes, Route } from "react-router-dom";
import { RunsProvider } from "@/components/RunsProvider";
import { Sidebar } from "@/components/Sidebar";
import Overview from "@/pages/Overview";
import WorkflowPage from "@/pages/WorkflowPage";
import RunsPage from "@/pages/RunsPage";
import RunDetail from "@/pages/RunDetail";
import ProfilesPage from "@/pages/ProfilesPage";
import DatasetsPage from "@/pages/DatasetsPage";
import DatasetPage from "@/pages/DatasetPage";
import AgentsPage from "@/pages/AgentsPage";
import AgentLaunchPage from "@/pages/AgentLaunchPage";
import AgentSessionPage from "@/pages/AgentSessionPage";

export default function App() {
  return (
    <RunsProvider>
      <div className="flex h-screen">
        <Sidebar />
        <main className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/workflows/:id" element={<WorkflowPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/agents/sessions/:id" element={<AgentSessionPage />} />
            <Route path="/agents/:id" element={<AgentLaunchPage />} />
            <Route path="/data" element={<DatasetsPage />} />
            <Route path="/data/:id" element={<DatasetPage />} />
            <Route path="/profiles" element={<ProfilesPage />} />
          </Routes>
        </main>
      </div>
    </RunsProvider>
  );
}
