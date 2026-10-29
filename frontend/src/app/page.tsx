import UploadPanel from "@/components/UploadPanel";
import s from "@/components/workspace.module.css";

export default function Home() {
  return (
    <>
      <div className={s.intro}>
        <p className={s.eyebrow}>The foundation for better analysis</p>
        <h1>
          Know your data.
          <br />
          Before you go further.
        </h1>
        <p className={s.lede}>
          Turn a CSV into a clear picture of its structure, completeness, and
          consistency. Start with what the data can tell you.
        </p>
      </div>
      <div className={s.uploadGrid}>
        <UploadPanel />
        <aside aria-labelledby="process-title">
          <h2 id="process-title" className={s.asideTitle}>
            From file to understanding.
          </h2>
          <ol className={s.steps}>
            <li className={s.step}>
              <span className={s.index}>01</span>
              <div>
                <h3>Bring a CSV</h3>
                <p>
                  Upload a file from your computer. The local engine detects its
                  encoding, delimiter, and column types.
                </p>
              </div>
            </li>
            <li className={s.step}>
              <span className={s.index}>02</span>
              <div>
                <h3>Review its health</h3>
                <p>
                  See missing cells, duplicate rows, and constant-column
                  warnings, with the scoring method explained.
                </p>
              </div>
            </li>
            <li className={s.step}>
              <span className={s.index}>03</span>
              <div>
                <h3>Inspect the details</h3>
                <p>
                  Explore column profiles and type-specific statistics. Return
                  to the dataset link without uploading again.
                </p>
              </div>
            </li>
          </ol>
          <p className={s.privacy}>
            No account required. No external analytics. A focused first look at
            your data.
          </p>
        </aside>
      </div>
    </>
  );
}
