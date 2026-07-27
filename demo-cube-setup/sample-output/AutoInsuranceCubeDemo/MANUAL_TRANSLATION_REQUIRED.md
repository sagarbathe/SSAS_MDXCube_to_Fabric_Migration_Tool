# Manual DAX Translation Required

The following MDX constructs were extracted from the source cube and require manual DAX translation before they can be added to the deployed semantic model:

- Calculated member: (see raw MDX script)
  - Original MDX: `CREATE MEMBER CURRENTCUBE.[Measures].[Loss Ratio] AS IIF([Measures].[Incurred Amount] = 0, NULL, [Measures].[Paid Amount] / [Measures].[Incurred Amount]), FORMAT_STRING = '0.00%', VISIBLE = 1;`
- Calculated member: (see raw MDX script)
  - Original MDX: `CREATE MEMBER CURRENTCUBE.[Measures].[Claim Severity] AS IIF([Measures].[Claim Count] = 0, NULL, [Measures].[Incurred Amount] / [Measures].[Claim Count]), FORMAT_STRING = 'Currency', VISIBLE = 1;`
- KPI: Loss Ratio KPI