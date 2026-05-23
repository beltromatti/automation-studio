import { Routes, Route } from "react-router-dom";
import { RunsProvider } from "@/components/RunsProvider";
import { Sidebar } from "@/components/Sidebar";
import Overview from "@/pages/Overview";
import WorkflowPage from "@/pages/WorkflowPage";
import RunsPage from "@/pages/RunsPage";
import RunDetail from "@/pages/RunDetail";

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
          </Routes>
        </main>
      </div>
    </RunsProvider>
  );
}
