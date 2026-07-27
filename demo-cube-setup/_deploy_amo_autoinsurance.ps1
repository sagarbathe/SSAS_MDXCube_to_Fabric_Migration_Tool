$ErrorActionPreference = "Stop"
$asPath = Get-ChildItem "C:\Program Files\Microsoft SQL Server\" -Recurse -Filter "Microsoft.AnalysisServices.dll" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
Add-Type -Path $asPath
Add-Type -AssemblyName System.Data

$serverName = "LAPTOP-LQVSA8VE\SSAS"

# ---------------- Grant SSAS service account read access on the new relational DB ----------------
$sqlcmd = "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\SQLCMD.EXE"
if (-not (Test-Path $sqlcmd)) { $sqlcmd = (Get-Command sqlcmd -ErrorAction SilentlyContinue).Source }
& $sqlcmd -E -C -S "LAPTOP-LQVSA8VE" -d AutoInsuranceDW -Q "IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'NT AUTHORITY\NETWORK SERVICE') BEGIN CREATE USER [NT AUTHORITY\NETWORK SERVICE] FOR LOGIN [NT AUTHORITY\NETWORK SERVICE]; END; EXEC sp_addrolemember 'db_datareader', 'NT AUTHORITY\NETWORK SERVICE';"

$srv = New-Object Microsoft.AnalysisServices.Server
$srv.Connect($serverName)

$existing = $srv.Databases.FindByName("AutoInsuranceCubeDemo")
if ($existing) { $existing.Drop(); Write-Host "Dropped existing AutoInsuranceCubeDemo database." }

$db = $srv.Databases.Add("AutoInsuranceCubeDemo")
Write-Host "Database object created in memory (not yet pushed to server)."

# ---------------- Data Source ----------------
$ds = $db.DataSources.Add("AutoInsuranceDW", "AutoInsuranceDW")
$ds.ConnectionString = "Provider=MSOLEDBSQL;Data Source=localhost;Integrated Security=SSPI;Initial Catalog=AutoInsuranceDW;Encrypt=True;TrustServerCertificate=True"
Write-Host "DataSource added in memory."

# ---------------- Data Source View (explicit deterministic schema) ----------------
$dataSet = New-Object System.Data.DataSet "AutoInsuranceDW"

function New-Table($name, [hashtable[]]$cols) {
    $t = New-Object System.Data.DataTable $name
    foreach ($c in $cols) { $t.Columns.Add($c.Name, $c.Type) | Out-Null }
    $dataSet.Tables.Add($t) | Out-Null
    return $t
}

New-Table "Dim_Date" @(
    @{Name="DateKey"; Type=[int]}, @{Name="FullDate"; Type=[datetime]},
    @{Name="Year"; Type=[int]}, @{Name="Quarter"; Type=[int]}, @{Name="Month"; Type=[int]},
    @{Name="MonthName"; Type=[string]}, @{Name="Day"; Type=[int]},
    @{Name="DayOfWeek"; Type=[int]}, @{Name="WeekdayName"; Type=[string]},
    @{Name="ParentDateKey"; Type=[int]}
) | Out-Null

New-Table "Dim_Geography" @(
    @{Name="GeographyKey"; Type=[int]}, @{Name="City"; Type=[string]}, @{Name="State"; Type=[string]},
    @{Name="ZipCode"; Type=[string]}, @{Name="Region"; Type=[string]},
    @{Name="Latitude"; Type=[double]}, @{Name="Longitude"; Type=[double]}
) | Out-Null

New-Table "Dim_ClaimType" @(
    @{Name="ClaimTypeKey"; Type=[int]}, @{Name="ClaimTypeName"; Type=[string]}, @{Name="ClaimCategory"; Type=[string]}
) | Out-Null

New-Table "Dim_Policyholder" @(
    @{Name="PolicyholderKey"; Type=[int]}, @{Name="PolicyholderID"; Type=[string]},
    @{Name="FirstName"; Type=[string]}, @{Name="LastName"; Type=[string]}, @{Name="Gender"; Type=[string]},
    @{Name="DateOfBirth"; Type=[datetime]}, @{Name="CreditScoreBand"; Type=[string]}, @{Name="GeographyKey"; Type=[int]}
) | Out-Null

New-Table "Dim_Policy" @(
    @{Name="PolicyKey"; Type=[int]}, @{Name="PolicyNumber"; Type=[string]}, @{Name="PolicyholderKey"; Type=[int]},
    @{Name="PolicyType"; Type=[string]}, @{Name="EffectiveDate"; Type=[datetime]}, @{Name="ExpirationDate"; Type=[datetime]},
    @{Name="VehicleMake"; Type=[string]}, @{Name="VehicleModel"; Type=[string]}, @{Name="VehicleYear"; Type=[int]},
    @{Name="CoverageLimit"; Type=[double]}, @{Name="DeductibleAmount"; Type=[double]}
) | Out-Null

New-Table "Fact_Claims" @(
    @{Name="ClaimKey"; Type=[int]}, @{Name="ClaimNumber"; Type=[string]}, @{Name="PolicyKey"; Type=[int]},
    @{Name="PolicyholderKey"; Type=[int]}, @{Name="ClaimTypeKey"; Type=[int]}, @{Name="GeographyKey"; Type=[int]},
    @{Name="DateOfLossKey"; Type=[int]}, @{Name="DateReportedKey"; Type=[int]}, @{Name="ClaimStatus"; Type=[string]},
    @{Name="IncurredAmount"; Type=[double]}, @{Name="PaidAmount"; Type=[double]}, @{Name="ReserveAmount"; Type=[double]},
    @{Name="DeductibleAmount"; Type=[double]}, @{Name="ClaimCount"; Type=[int]}
) | Out-Null

# Foreign key relations (helps SSAS infer dimension usage)
$dataSet.Relations.Add("FK_Claims_Policy", $dataSet.Tables["Dim_Policy"].Columns["PolicyKey"], $dataSet.Tables["Fact_Claims"].Columns["PolicyKey"]) | Out-Null
$dataSet.Relations.Add("FK_Claims_Policyholder", $dataSet.Tables["Dim_Policyholder"].Columns["PolicyholderKey"], $dataSet.Tables["Fact_Claims"].Columns["PolicyholderKey"]) | Out-Null
$dataSet.Relations.Add("FK_Claims_ClaimType", $dataSet.Tables["Dim_ClaimType"].Columns["ClaimTypeKey"], $dataSet.Tables["Fact_Claims"].Columns["ClaimTypeKey"]) | Out-Null
$dataSet.Relations.Add("FK_Claims_Geography", $dataSet.Tables["Dim_Geography"].Columns["GeographyKey"], $dataSet.Tables["Fact_Claims"].Columns["GeographyKey"]) | Out-Null
$dataSet.Relations.Add("FK_Claims_DateOfLoss", $dataSet.Tables["Dim_Date"].Columns["DateKey"], $dataSet.Tables["Fact_Claims"].Columns["DateOfLossKey"]) | Out-Null
$dataSet.Relations.Add("FK_Policy_Policyholder", $dataSet.Tables["Dim_Policyholder"].Columns["PolicyholderKey"], $dataSet.Tables["Dim_Policy"].Columns["PolicyholderKey"]) | Out-Null
$dataSet.Relations.Add("FK_Policyholder_Geography", $dataSet.Tables["Dim_Geography"].Columns["GeographyKey"], $dataSet.Tables["Dim_Policyholder"].Columns["GeographyKey"]) | Out-Null

$dsv = $db.DataSourceViews.Add("AutoInsuranceDW", "AutoInsuranceDW")
$dsv.DataSourceID = "AutoInsuranceDW"
$dsv.Schema = $dataSet
Write-Host "DataSourceView added in memory with explicit schema."

# ---------------- Dimensions ----------------
function New-Dimension($id, $name, $tableName, $keyCol, $nameCol) {
    $dim = $db.Dimensions.Add($id, $name)
    $dim.Source = New-Object Microsoft.AnalysisServices.DataSourceViewBinding($dsv.ID)
    $attr = $dim.Attributes.Add($id, $name)
    $attr.Usage = [Microsoft.AnalysisServices.AttributeUsage]::Key
    $attr.KeyColumns.Add((New-Object Microsoft.AnalysisServices.ColumnBinding($tableName, $keyCol))) | Out-Null
    if ($nameCol) {
        $attr.NameColumn = New-Object Microsoft.AnalysisServices.ColumnBinding($tableName, $nameCol)
    }
    return $dim
}

function Add-Attribute($dim, $id, $name, $tableName, $col) {
    $attr = $dim.Attributes.Add($id, $name)
    $attr.KeyColumns.Add((New-Object Microsoft.AnalysisServices.ColumnBinding($tableName, $col))) | Out-Null
    return $attr
}

# Dim Date
$dimDate = New-Dimension "Dim Date" "Dim Date" "Dim_Date" "DateKey" "FullDate"
Add-Attribute $dimDate "Year" "Year" "Dim_Date" "Year" | Out-Null
Add-Attribute $dimDate "Quarter" "Quarter" "Dim_Date" "Quarter" | Out-Null
$monthAttr = Add-Attribute $dimDate "Month Name" "Month Name" "Dim_Date" "Month"
$monthAttr.NameColumn = New-Object Microsoft.AnalysisServices.ColumnBinding("Dim_Date","MonthName")
Add-Attribute $dimDate "Day Of Month" "Day Of Month" "Dim_Date" "Day" | Out-Null

$hierDate = $dimDate.Hierarchies.Add("Calendar","Calendar")
$hierDate.Levels.Add("Year","Year").SourceAttributeID = "Year"
$hierDate.Levels.Add("Quarter","Quarter").SourceAttributeID = "Quarter"
$hierDate.Levels.Add("Month Name","Month Name").SourceAttributeID = "Month Name"
$hierDate.Levels.Add("Day Of Month","Day Of Month").SourceAttributeID = "Day Of Month"

# ---- Parent-child hierarchy (Day -> Month-End -> Quarter-End -> Year-End -> root) ----
# This is a genuine MDX/AMO parent-child attribute: its own members ARE the
# dimension's date members, linked via the self-referencing ParentDateKey
# column. Direct Lake / DAX has no PATH()-based calculated-column equivalent,
# so this is deliberately included to exercise the migration tool's
# parent-child detection and Migration Conversion Report guidance.
$pcAttr = $dimDate.Attributes.Add("Date Rollup", "Date Rollup")
$pcAttr.Usage = [Microsoft.AnalysisServices.AttributeUsage]::Parent
$pcAttr.KeyColumns.Add((New-Object Microsoft.AnalysisServices.ColumnBinding("Dim_Date","ParentDateKey"))) | Out-Null
$pcAttr.NameColumn = New-Object Microsoft.AnalysisServices.ColumnBinding("Dim_Date","FullDate")
$pcAttr.RootMemberIf = [Microsoft.AnalysisServices.RootIfValue]::ParentIsBlankSelfOrMissing
Write-Host "Dim Date parent-child attribute (Date Rollup) added in memory."
Write-Host "Dim Date added in memory."

# Dim Geography (with coordinates)
$dimGeo = New-Dimension "Dim Geography" "Dim Geography" "Dim_Geography" "GeographyKey" "City"
Add-Attribute $dimGeo "State" "State" "Dim_Geography" "State" | Out-Null
Add-Attribute $dimGeo "Region" "Region" "Dim_Geography" "Region" | Out-Null
Add-Attribute $dimGeo "Zip Code" "Zip Code" "Dim_Geography" "ZipCode" | Out-Null
Add-Attribute $dimGeo "Latitude" "Latitude" "Dim_Geography" "Latitude" | Out-Null
Add-Attribute $dimGeo "Longitude" "Longitude" "Dim_Geography" "Longitude" | Out-Null

$hierGeo = $dimGeo.Hierarchies.Add("Geography","Geography")
$hierGeo.Levels.Add("Region","Region").SourceAttributeID = "Region"
$hierGeo.Levels.Add("State","State").SourceAttributeID = "State"
$hierGeo.Levels.Add("City","City").SourceAttributeID = "Dim Geography"
Write-Host "Dim Geography added in memory."

# Dim Claim Type
$dimClaimType = New-Dimension "Dim Claim Type" "Dim Claim Type" "Dim_ClaimType" "ClaimTypeKey" "ClaimTypeName"
Add-Attribute $dimClaimType "Claim Category" "Claim Category" "Dim_ClaimType" "ClaimCategory" | Out-Null

$hierCT = $dimClaimType.Hierarchies.Add("Claim Category Detail","Claim Category Detail")
$hierCT.Levels.Add("Claim Category","Claim Category").SourceAttributeID = "Claim Category"
$hierCT.Levels.Add("Claim Type","Claim Type").SourceAttributeID = "Dim Claim Type"
Write-Host "Dim Claim Type added in memory."

# Dim Policyholder
$dimPH = New-Dimension "Dim Policyholder" "Dim Policyholder" "Dim_Policyholder" "PolicyholderKey" "PolicyholderID"
Add-Attribute $dimPH "First Name" "First Name" "Dim_Policyholder" "FirstName" | Out-Null
Add-Attribute $dimPH "Last Name" "Last Name" "Dim_Policyholder" "LastName" | Out-Null
Add-Attribute $dimPH "Gender" "Gender" "Dim_Policyholder" "Gender" | Out-Null
Add-Attribute $dimPH "Date Of Birth" "Date Of Birth" "Dim_Policyholder" "DateOfBirth" | Out-Null
Add-Attribute $dimPH "Credit Score Band" "Credit Score Band" "Dim_Policyholder" "CreditScoreBand" | Out-Null
Write-Host "Dim Policyholder added in memory."

# Dim Policy
$dimPolicy = New-Dimension "Dim Policy" "Dim Policy" "Dim_Policy" "PolicyKey" "PolicyNumber"
Add-Attribute $dimPolicy "Policy Type" "Policy Type" "Dim_Policy" "PolicyType" | Out-Null
Add-Attribute $dimPolicy "Vehicle Make" "Vehicle Make" "Dim_Policy" "VehicleMake" | Out-Null
Add-Attribute $dimPolicy "Vehicle Model" "Vehicle Model" "Dim_Policy" "VehicleModel" | Out-Null
Add-Attribute $dimPolicy "Vehicle Year" "Vehicle Year" "Dim_Policy" "VehicleYear" | Out-Null
Add-Attribute $dimPolicy "Effective Date" "Effective Date" "Dim_Policy" "EffectiveDate" | Out-Null
Add-Attribute $dimPolicy "Expiration Date" "Expiration Date" "Dim_Policy" "ExpirationDate" | Out-Null
Add-Attribute $dimPolicy "Coverage Limit" "Coverage Limit" "Dim_Policy" "CoverageLimit" | Out-Null
Add-Attribute $dimPolicy "Policy Deductible Amount" "Policy Deductible Amount" "Dim_Policy" "DeductibleAmount" | Out-Null
Write-Host "Dim Policy added in memory."

# ---------------- Cube ----------------
$cube = $db.Cubes.Add("Auto Insurance Cube", "Auto Insurance Cube")
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Date","Dim Date","Dim Date"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Geography","Dim Geography","Dim Geography"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Claim Type","Dim Claim Type","Dim Claim Type"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Policyholder","Dim Policyholder","Dim Policyholder"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Policy","Dim Policy","Dim Policy"))) | Out-Null

$mg = $cube.MeasureGroups.Add("Fact Claims","Fact Claims")

function Add-Measure($mg, $id, $name, $col, $oleDbType, $measureType, $aggFn) {
    $m = $mg.Measures.Add($id, $name)
    $m.Source = New-Object Microsoft.AnalysisServices.ColumnBinding("Fact_Claims", $col)
    $m.Source.DataType = $oleDbType
    $m.DataType = $measureType
    $m.AggregateFunction = $aggFn
    return $m
}

Add-Measure $mg "Incurred Amount" "Incurred Amount" "IncurredAmount" ([System.Data.OleDb.OleDbType]::Double) ([Microsoft.AnalysisServices.MeasureDataType]::Double) ([Microsoft.AnalysisServices.AggregationFunction]::Sum) | Out-Null
Add-Measure $mg "Paid Amount" "Paid Amount" "PaidAmount" ([System.Data.OleDb.OleDbType]::Double) ([Microsoft.AnalysisServices.MeasureDataType]::Double) ([Microsoft.AnalysisServices.AggregationFunction]::Sum) | Out-Null
Add-Measure $mg "Reserve Amount" "Reserve Amount" "ReserveAmount" ([System.Data.OleDb.OleDbType]::Double) ([Microsoft.AnalysisServices.MeasureDataType]::Double) ([Microsoft.AnalysisServices.AggregationFunction]::Sum) | Out-Null
Add-Measure $mg "Deductible Amount" "Deductible Amount" "DeductibleAmount" ([System.Data.OleDb.OleDbType]::Double) ([Microsoft.AnalysisServices.MeasureDataType]::Double) ([Microsoft.AnalysisServices.AggregationFunction]::Sum) | Out-Null
Add-Measure $mg "Claim Count" "Claim Count" "ClaimCount" ([System.Data.OleDb.OleDbType]::Integer) ([Microsoft.AnalysisServices.MeasureDataType]::Integer) ([Microsoft.AnalysisServices.AggregationFunction]::Sum) | Out-Null

function Add-MgDimension($mg, $cubeDimId, $attrId, $factCol) {
    $mgDim = New-Object Microsoft.AnalysisServices.RegularMeasureGroupDimension($cubeDimId)
    $mgAttr = New-Object Microsoft.AnalysisServices.MeasureGroupAttribute($attrId)
    $mgAttr.Type = [Microsoft.AnalysisServices.MeasureGroupAttributeType]::Granularity
    $mgAttr.KeyColumns.Add((New-Object Microsoft.AnalysisServices.ColumnBinding("Fact_Claims",$factCol))) | Out-Null
    $mgDim.Attributes.Add($mgAttr) | Out-Null
    $mg.Dimensions.Add($mgDim) | Out-Null
}
Add-MgDimension $mg "Dim Date" "Dim Date" "DateOfLossKey"
Add-MgDimension $mg "Dim Geography" "Dim Geography" "GeographyKey"
Add-MgDimension $mg "Dim Claim Type" "Dim Claim Type" "ClaimTypeKey"
Add-MgDimension $mg "Dim Policyholder" "Dim Policyholder" "PolicyholderKey"
Add-MgDimension $mg "Dim Policy" "Dim Policy" "PolicyKey"

$partition = $mg.Partitions.Add("Fact Claims","Fact Claims")
$partition.Source = New-Object Microsoft.AnalysisServices.DsvTableBinding($dsv.ID, "Fact_Claims")

# ---------------- Calculated members (MDX, not auto-translatable to DAX) ----------------
if ($cube.MdxScripts.Count -eq 0) { $mdxScript = $cube.MdxScripts.Add("MdxScript") } else { $mdxScript = $cube.MdxScripts[0] }

$cmdLossRatio = New-Object Microsoft.AnalysisServices.Command
$cmdLossRatio.Text = "CREATE MEMBER CURRENTCUBE.[Measures].[Loss Ratio] AS IIF([Measures].[Incurred Amount] = 0, NULL, [Measures].[Paid Amount] / [Measures].[Incurred Amount]), FORMAT_STRING = '0.00%', VISIBLE = 1;"
$mdxScript.Commands.Add($cmdLossRatio) | Out-Null

$cmdSeverity = New-Object Microsoft.AnalysisServices.Command
$cmdSeverity.Text = "CREATE MEMBER CURRENTCUBE.[Measures].[Claim Severity] AS IIF([Measures].[Claim Count] = 0, NULL, [Measures].[Incurred Amount] / [Measures].[Claim Count]), FORMAT_STRING = 'Currency', VISIBLE = 1;"
$mdxScript.Commands.Add($cmdSeverity) | Out-Null
Write-Host "Calculated members (Loss Ratio, Claim Severity) added in memory."

# ---------------- KPI (Goal/Status/Trend MDX expressions - no direct DAX KPI equivalent) ----------------
$kpi = New-Object Microsoft.AnalysisServices.Kpi
$kpi.Name = "Loss Ratio KPI"
$kpi.AssociatedMeasureGroupID = $mg.ID
$kpi.Value = "[Measures].[Loss Ratio]"
$kpi.Goal = "0.65"
$kpi.Status = "CASE WHEN [Measures].[Loss Ratio] <= 0.65 THEN 1 WHEN [Measures].[Loss Ratio] <= 0.80 THEN 0 ELSE -1 END"
$kpi.Trend = "0"
$kpi.StatusGraphic = "Traffic Light"
$kpi.TrendGraphic = "Standard Arrow"
$cube.Kpis.Add($kpi) | Out-Null
Write-Host "KPI (Loss Ratio KPI) added in memory."

Write-Host "Cube built in memory. Pushing entire database tree to server with ExpandFull..."

# ---------------- Single consolidated update ----------------
$db.Update([Microsoft.AnalysisServices.UpdateOptions]::ExpandFull)
Write-Host "Database, data source, DSV, dimensions and cube created successfully on server."

# ---------------- Process ----------------
$db.Process([Microsoft.AnalysisServices.ProcessType]::ProcessFull)
Write-Host "Database processed (Process Full) successfully."

$srv.Disconnect()
Write-Host "Done."
