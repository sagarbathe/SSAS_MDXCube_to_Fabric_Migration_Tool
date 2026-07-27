$ErrorActionPreference = "Stop"
$asPath = Get-ChildItem "C:\Program Files\Microsoft SQL Server\" -Recurse -Filter "Microsoft.AnalysisServices.dll" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
Add-Type -Path $asPath
Add-Type -AssemblyName System.Data

$serverName = "LAPTOP-LQVSA8VE\SSAS"
$sqlConnStr = "Data Source=localhost;Initial Catalog=RetailDW;Integrated Security=SSPI;Encrypt=True;TrustServerCertificate=True"

$srv = New-Object Microsoft.AnalysisServices.Server
$srv.Connect($serverName)

$existing = $srv.Databases.FindByName("RetailCubeDemo")
if ($existing) { $existing.Drop(); Write-Host "Dropped existing RetailCubeDemo database." }

$db = $srv.Databases.Add("RetailCubeDemo")
Write-Host "Database object created in memory (not yet pushed to server)."

# ---------------- Data Source ----------------
$ds = $db.DataSources.Add("RetailDW", "RetailDW")
$ds.ConnectionString = "Provider=MSOLEDBSQL;Data Source=localhost;Integrated Security=SSPI;Initial Catalog=RetailDW;Encrypt=True;TrustServerCertificate=True"
Write-Host "DataSource added in memory."

# ---------------- Data Source View (explicit deterministic schema, matching RetailDW star schema) ----------------
$dataSet = New-Object System.Data.DataSet "RetailDW"

function New-Table($name, [hashtable[]]$cols) {
    $t = New-Object System.Data.DataTable $name
    foreach ($c in $cols) { $t.Columns.Add($c.Name, $c.Type) | Out-Null }
    $dataSet.Tables.Add($t) | Out-Null
    return $t
}

New-Table "DimDate" @(
    @{Name="DateKey"; Type=[int]}, @{Name="FullDate"; Type=[datetime]},
    @{Name="DayOfMonth"; Type=[int]}, @{Name="MonthNumber"; Type=[int]},
    @{Name="MonthName"; Type=[string]}, @{Name="Quarter"; Type=[int]}, @{Name="Year"; Type=[int]}
) | Out-Null

New-Table "DimProduct" @(
    @{Name="ProductKey"; Type=[int]}, @{Name="ProductName"; Type=[string]},
    @{Name="Category"; Type=[string]}, @{Name="SubCategory"; Type=[string]}, @{Name="UnitPrice"; Type=[double]}
) | Out-Null

New-Table "DimCustomer" @(
    @{Name="CustomerKey"; Type=[int]}, @{Name="CustomerName"; Type=[string]},
    @{Name="City"; Type=[string]}, @{Name="Country"; Type=[string]}
) | Out-Null

New-Table "FactSales" @(
    @{Name="SalesID"; Type=[int]}, @{Name="DateKey"; Type=[int]}, @{Name="ProductKey"; Type=[int]},
    @{Name="CustomerKey"; Type=[int]}, @{Name="Quantity"; Type=[int]},
    @{Name="SalesAmount"; Type=[double]}, @{Name="CostAmount"; Type=[double]}
) | Out-Null

# Foreign key relations (helps SSAS infer dimension usage / auto-relationships)
$dataSet.Relations.Add("FK_FactSales_DimDate", $dataSet.Tables["DimDate"].Columns["DateKey"], $dataSet.Tables["FactSales"].Columns["DateKey"]) | Out-Null
$dataSet.Relations.Add("FK_FactSales_DimProduct", $dataSet.Tables["DimProduct"].Columns["ProductKey"], $dataSet.Tables["FactSales"].Columns["ProductKey"]) | Out-Null
$dataSet.Relations.Add("FK_FactSales_DimCustomer", $dataSet.Tables["DimCustomer"].Columns["CustomerKey"], $dataSet.Tables["FactSales"].Columns["CustomerKey"]) | Out-Null

$dsv = $db.DataSourceViews.Add("RetailDW", "RetailDW")
$dsv.DataSourceID = "RetailDW"
$dsv.Schema = $dataSet
Write-Host "DataSourceView added in memory with live schema."

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
$dimDate = New-Dimension "Dim Date" "Dim Date" "DimDate" "DateKey" "FullDate"
Add-Attribute $dimDate "Year" "Year" "DimDate" "Year" | Out-Null
Add-Attribute $dimDate "Quarter" "Quarter" "DimDate" "Quarter" | Out-Null
$monthAttr = Add-Attribute $dimDate "Month Name" "Month Name" "DimDate" "MonthNumber"
$monthAttr.NameColumn = New-Object Microsoft.AnalysisServices.ColumnBinding("DimDate","MonthName")
Add-Attribute $dimDate "Day Of Month" "Day Of Month" "DimDate" "DayOfMonth" | Out-Null

$hier = $dimDate.Hierarchies.Add("Calendar","Calendar")
$hier.Levels.Add("Year","Year").SourceAttributeID = "Year"
$hier.Levels.Add("Quarter","Quarter").SourceAttributeID = "Quarter"
$hier.Levels.Add("Month Name","Month Name").SourceAttributeID = "Month Name"
$hier.Levels.Add("Day Of Month","Day Of Month").SourceAttributeID = "Day Of Month"
Write-Host "Dim Date added in memory."

# Dim Product
$dimProduct = New-Dimension "Dim Product" "Dim Product" "DimProduct" "ProductKey" "ProductName"
Add-Attribute $dimProduct "Category" "Category" "DimProduct" "Category" | Out-Null
Add-Attribute $dimProduct "Sub Category" "Sub Category" "DimProduct" "SubCategory" | Out-Null
Add-Attribute $dimProduct "Unit Price" "Unit Price" "DimProduct" "UnitPrice" | Out-Null

$hierP = $dimProduct.Hierarchies.Add("Category Detail","Category Detail")
$hierP.Levels.Add("Category","Category").SourceAttributeID = "Category"
$hierP.Levels.Add("Sub Category","Sub Category").SourceAttributeID = "Sub Category"
$hierP.Levels.Add("Product Key","Product").SourceAttributeID = "Dim Product"
Write-Host "Dim Product added in memory."

# Dim Customer
$dimCustomer = New-Dimension "Dim Customer" "Dim Customer" "DimCustomer" "CustomerKey" "CustomerName"
Add-Attribute $dimCustomer "Country" "Country" "DimCustomer" "Country" | Out-Null
Add-Attribute $dimCustomer "City" "City" "DimCustomer" "City" | Out-Null

$hierC = $dimCustomer.Hierarchies.Add("Geography","Geography")
$hierC.Levels.Add("Country","Country").SourceAttributeID = "Country"
$hierC.Levels.Add("City","City").SourceAttributeID = "City"
$hierC.Levels.Add("Customer Key","Customer").SourceAttributeID = "Dim Customer"
Write-Host "Dim Customer added in memory."

# ---------------- Cube ----------------
$cube = $db.Cubes.Add("Sales Cube", "Sales Cube")
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Date","Dim Date","Dim Date"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Product","Dim Product","Dim Product"))) | Out-Null
$cube.Dimensions.Add((New-Object Microsoft.AnalysisServices.CubeDimension("Dim Customer","Dim Customer","Dim Customer"))) | Out-Null

$mg = $cube.MeasureGroups.Add("Fact Sales","Fact Sales")

$m1 = $mg.Measures.Add("Sales Amount","Sales Amount")
$m1.Source = New-Object Microsoft.AnalysisServices.ColumnBinding("FactSales","SalesAmount")
$m1.Source.DataType = [System.Data.OleDb.OleDbType]::Double
$m1.DataType = [Microsoft.AnalysisServices.MeasureDataType]::Double
$m1.AggregateFunction = [Microsoft.AnalysisServices.AggregationFunction]::Sum

$m2 = $mg.Measures.Add("Cost Amount","Cost Amount")
$m2.Source = New-Object Microsoft.AnalysisServices.ColumnBinding("FactSales","CostAmount")
$m2.Source.DataType = [System.Data.OleDb.OleDbType]::Double
$m2.DataType = [Microsoft.AnalysisServices.MeasureDataType]::Double
$m2.AggregateFunction = [Microsoft.AnalysisServices.AggregationFunction]::Sum

$m3 = $mg.Measures.Add("Quantity","Quantity")
$m3.Source = New-Object Microsoft.AnalysisServices.ColumnBinding("FactSales","Quantity")
$m3.Source.DataType = [System.Data.OleDb.OleDbType]::Integer
$m3.DataType = [Microsoft.AnalysisServices.MeasureDataType]::Integer
$m3.AggregateFunction = [Microsoft.AnalysisServices.AggregationFunction]::Sum

$m4 = $mg.Measures.Add("Sales Count","Sales Count")
$m4.Source = New-Object Microsoft.AnalysisServices.ColumnBinding("FactSales","SalesID")
$m4.Source.DataType = [System.Data.OleDb.OleDbType]::Integer
$m4.DataType = [Microsoft.AnalysisServices.MeasureDataType]::Integer
$m4.AggregateFunction = [Microsoft.AnalysisServices.AggregationFunction]::Count

function Add-MgDimension($mg, $cubeDimId, $attrId, $factCol) {
    $mgDim = New-Object Microsoft.AnalysisServices.RegularMeasureGroupDimension($cubeDimId)
    $mgAttr = New-Object Microsoft.AnalysisServices.MeasureGroupAttribute($attrId)
    $mgAttr.Type = [Microsoft.AnalysisServices.MeasureGroupAttributeType]::Granularity
    $mgAttr.KeyColumns.Add((New-Object Microsoft.AnalysisServices.ColumnBinding("FactSales",$factCol))) | Out-Null
    $mgDim.Attributes.Add($mgAttr) | Out-Null
    $mg.Dimensions.Add($mgDim) | Out-Null
}
Add-MgDimension $mg "Dim Date" "Dim Date" "DateKey"
Add-MgDimension $mg "Dim Product" "Dim Product" "ProductKey"
Add-MgDimension $mg "Dim Customer" "Dim Customer" "CustomerKey"

$partition = $mg.Partitions.Add("Fact Sales","Fact Sales")
$partition.Source = New-Object Microsoft.AnalysisServices.DsvTableBinding($dsv.ID, "FactSales")

Write-Host "Cube built in memory. Pushing entire database tree to server with ExpandFull..."

# ---------------- Single consolidated update: pushes DB + DataSource + DSV + all Dimensions + Cube ----------------
$db.Update([Microsoft.AnalysisServices.UpdateOptions]::ExpandFull)
Write-Host "Database, data source, DSV, dimensions and cube created successfully on server."

# ---------------- Process ----------------
$db.Process([Microsoft.AnalysisServices.ProcessType]::ProcessFull)
Write-Host "Database processed (Process Full) successfully."

$srv.Disconnect()
