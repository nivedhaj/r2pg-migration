# Build stage (.NET 10)
FROM mcr.microsoft.com/dotnet/sdk:10.0-preview AS build
WORKDIR /src

COPY ["StudentFeePoc.csproj", "./"]
RUN dotnet restore "StudentFeePoc.csproj"

COPY . .
RUN dotnet publish "StudentFeePoc.csproj" -c Release -o /app/publish /p:UseAppHost=false

# Runtime stage (.NET 10)
FROM mcr.microsoft.com/dotnet/aspnet:10.0-preview AS final
WORKDIR /app
COPY --from=build /app/publish .

ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080

ENTRYPOINT ["dotnet", "StudentFeePoc.dll"]
