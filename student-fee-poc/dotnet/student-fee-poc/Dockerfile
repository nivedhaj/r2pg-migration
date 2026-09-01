# Build stage (.NET 10)
FROM mcr.microsoft.com/dotnet/sdk:10.0-preview AS build
WORKDIR /src

COPY ["student-fee-poc.csproj", "./"]
RUN dotnet restore "student-fee-poc.csproj"

COPY . .
RUN dotnet publish "student-fee-poc.csproj" -c Release -o /app/publish /p:UseAppHost=false

# Runtime stage (.NET 10)
FROM mcr.microsoft.com/dotnet/aspnet:10.0-preview AS final
WORKDIR /app
COPY --from=build /app/publish .

ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080

ENTRYPOINT ["dotnet", "student-fee-poc.dll"]
