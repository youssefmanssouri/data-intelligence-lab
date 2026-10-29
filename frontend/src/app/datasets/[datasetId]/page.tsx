import Dashboard from "@/components/Dashboard";
export default async function DatasetPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = await params;
  return <Dashboard key={datasetId} datasetId={datasetId} />;
}
