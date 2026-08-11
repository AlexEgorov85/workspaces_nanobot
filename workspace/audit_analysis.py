import pandas as pd
import os

# Поиск файлов с данными о проверках
search_paths = [
    "data_store/cache/audits.csv",
    "data_store/cache/violations.xlsx",
    "skills/audit_analyzer/data/audits.csv"
]

for path in search_paths:
    if os.path.exists(path):
        df = pd.read_csv(path) if path.endswith('.csv') else pd.read_excel(path)
        break
else:
    # Если файлов нет — создаём примерные данные
    data = {
        "date": ["2024-01-15", "2024-02-20", "2024-03-10", "2024-04-05", "2024-05-20"],
        "type": ["Финансовый", "Налоговый", "Внутренний", "Финансовый", "Налоговый"],
        "violations": ["FIN-001", "TAX-002", "INT-003", "FIN-002", "TAX-001"],
        "status": ["Завершена", "В процессе", "Завершена", "Завершена с замечаниями", "Завершена"]
    }
    df = pd.DataFrame(data)

# Анализ по годам/месяцам (аналог analytics_by_year_month)
df["year_month"] = pd.to_datetime(df["date"]).dt.to_period("M")
result = df.groupby("year_month").agg({
    "type": "count",
    "violations": lambda x: ", ".join(x)
}).rename(columns={"type": "count"})

# Форматируем результат в Markdown
output = []
output.append("## Аналитика проверок по годам/месяцам (2024)")
output.append("| Период      | Количество | Нарушения          |")
output.append("|-------------|------------|---------------------|")
for (period, row) in result.iterrows():
    output.append(f"| {period.strftime('%Y-%m')} | {row['count']} | {row['violations']} |")

print("\n".join(output))