import UploadPanel from "@/components/UploadPanel";
import s from "@/components/workspace.module.css";

export default function Home() {
  return (
    <>
      <div className={s.hero}>
        <div className={`${s.card} ${s.intro}`}>
          <div>
            <p className={s.eyebrow}>The foundation for better analysis</p>
            <h1>
              Know your data.
              <br />
              Before you go further.
            </h1>
            <p className={s.lede}>
              Turn a CSV into a clear picture of its structure, completeness,
              and consistency. Start with what the data can tell you.
            </p>
          </div>
          <dl className={s.highlights}>
            <div>
              <dt>Detects</dt>
              <dd>Encoding & delimiter</dd>
            </div>
            <div>
              <dt>Infers</dt>
              <dd>Column types</dd>
            </div>
            <div>
              <dt>Scores</dt>
              <dd>Structural health</dd>
            </div>
          </dl>
        </div>
        <UploadPanel />
      </div>
      <div className={s.uploadGrid}>
        <section className={s.card} aria-labelledby="process-title">
          <div className={s.cardHead}>
            <h2 id="process-title">From file to understanding.</h2>
          </div>
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
        </section>
        <section className={s.card} aria-labelledby="method-title">
          <div className={s.cardHead}>
            <h2 id="method-title">How the health score works</h2>
          </div>
          <div className={`${s.breakdown} ${s.flat}`}>
            {[
              ["Completeness", 60, "Proportion of populated cells."],
              ["Row uniqueness", 30, "Proportion of distinct raw rows."],
              ["Column variation", 10, "Penalises constant columns."],
            ].map(([label, weight, note]) => (
              <div key={label}>
                <p className={s.scoreTitle}>
                  {label}
                  <span>{weight}% weight</span>
                </p>
                <span className={s.bar} aria-hidden="true">
                  <span style={{ width: `${weight}%` }} />
                </span>
                <p className={s.scoreText}>{note}</p>
              </div>
            ))}
          </div>
          <p className={s.privacy}>
            No account required. No external analytics. A focused first look at
            your data.
          </p>
        </section>
      </div>
    </>
  );
}
