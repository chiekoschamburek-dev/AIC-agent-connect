{
  "aic": "1.2.156.3088.0001.00001.FW49KF.06LGSK.1.0FVY", // 智能体唯一身份标识符，由注册服务分配。
  "active": true, // 智能体的激活状态，由注册服务维护。
  "lastModifiedTime": "2025-03-15T16:30:00+08:00", // 智能体能力描述的最后修改时间，由注册服务提供。采用 ISO 8601 格式，包含时区偏移信息，推荐使用北京时间（UTC+8）。
  "protocolVersion": "02.02", // 此智能体支持的 ACPs 协议版本，用于协议兼容性检查和版本匹配。
  "name": "北京城区旅游规划助手", // 此智能体的名称，简洁明了地描述智能体的主要功能。
  "description": "对北京城区旅游的规划和建议。只负责城六区，不负责郊区。北京城区范围旅游景点探索规划代理，专门负责北京城六区（东城/西城/朝阳/海淀/丰台/石景山）的旅游景点推荐和行程规划。拒绝超出城区范围的请求。", // 此智能体的详细描述，帮助用户和其他智能体理解其用途和限制。应明确说明智能体能做什么和不能做什么，包括地理范围、服务类型等限制。
  "version": "1.0.0", // 智能体的版本号，智能体提供者自行定义格式。推荐遵循语义化版本控制规范（Semantic Versioning）。格式：MAJOR.MINOR.PATCH，当 API 不兼容时递增 MAJOR 版本。
  "iconUrl": "https://example.com/icons/beijing-agent.png", // 智能体图标的 URL 地址，用于在用户界面中显示智能体标识。
  "documentationUrl": "https://docs.example.com/agents/beijing-tour", // 智能体详细文档的 URL 地址，提供使用说明和 API 文档。
  "webAppUrl": "https://demo.example.com/beijing-tour", // 智能体能力展示的 Web 应用 URL，用户可以通过此地址体验智能体功能。
  // 智能体服务提供者信息对象。定义智能体的开发和维护组织信息，包括组织身份、联系方式和合规资质。用于建立信任关系和提供技术支持联系渠道。
  "provider": {
    "countryCode": "CN", // 智能体提供者的国家或地区代码。符合 ISO 3166-1 alpha-2 标准。
    "organization": "ACPs工作组", // 智能体提供者的组织名称，通常为公司、大学或研究机构等顶级组织。
    "department": "人工智能学院", // 智能体提供者的具体部门或院系名称，提供更精确的组织结构信息。
    "url": "https://example.ioa.pub", // 智能体提供者的官方网站或相关文档的 URL 地址。
    "license": "string", // 智能体提供者的法律备案信息或许可证号，用于合规性验证。通常为 URL 对应网站的 ICP 备案号或其他相关资质证明。
    "name": "张三", // 智能体提供者的联系人姓名，便于技术支持和沟通。
    "email": "zhangsan@example.com", // 智能体提供者的联系人电子邮箱地址。
    // 智能体提供者的域名注册备案信息列表。记录 Provider 所使用的各个域名对应的注册备案信息，用于审核阶段验证域名归属。ACS 中所有 URL（endpoint、provider.url、documentationUrl、webAppUrl 等）的域名必须属于此列表中某个主域名的子域名或与之相同。
    "domainRegistrations": [
      {
        "domain": "example.com", // 主域名/注册域名（如 `example.com`），不含协议、路径和子域名前缀。
        "registrationNumber": "京ICP备140xxxxx号-1", // 备案号/注册号。ICP 类型为中国境内域名的 ICP 备案号（如 `京ICP备140xxxxx号-1`）。WHOIS 类型为 WHOIS 登记查询的注册信息标识。
        "registrationType": "ICP" // 注册类型。ICP: 中国境内域名的 ICP 备案。WHOIS: 国际域名的 WHOIS 登记信息查询，覆盖所有国际通用和国别域名。
      }
    ]
  },
  // 用于授权请求的可用安全方案声明。键是方案名称，值是对应的安全方案配置。遵循 OpenAPI 3.0 安全方案对象规范。智能体可以声明支持多种安全方案，在实际调用时根据端点的 security 配置选择合适的认证方式。
  "securitySchemes": {
    "mtls": {
      "type": "mutualTLS",
      "description": "智能体间 mTLS 双向认证"
    },
    "oidc": {
      "type": "openIdConnect",
      "description": "用户身份认证",
      "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"
    }
  },
  // 智能体端点配置列表，定义智能体可访问的服务端点信息。每个端点包含 URL、传输协议和安全要求。多个端点应该支持相同的业务功能，支持不同的协议和认证方式，以满足多样化的访问需求。
  "endPoints": [
    {
      "url": "string",
      "transport": "JSONRPC", // 此端点支持的传输协议类型。不同协议有不同的调用方式和 URL 解释。
      // 适用于此端点的安全要求配置列表。遵循 OpenAPI 3.0 安全要求对象规范。外层数组为 OR 关系，内层对象为 AND 关系。
      "security": [
        {}
      ]
    }
  ],
  // 智能体可选技术能力配置对象。定义智能体支持的高级功能特性，如实时通信、异步通知和消息队列集成。
  "capabilities": {
    "streaming": true, // 智能体是否支持 Server Send Event（SSE）用于流式响应。启用后可以实现实时数据推送和渐进式内容生成。
    "notification": true, // 智能体是否支持异步推送通知。启用后可以主动向指定 URL 推送事件和状态更新。
    // 智能体支持的消息队列能力配置。使用协议版本表达式字符串数组进行配置。空数组表示不支持任何消息队列协议。
    "messageQueue": [
      "kafka:>=2.8 <4.0"
    ]
  },
  // 所有技能的默认支持输入 MIME 类型集合，可在每个技能的基础上覆盖。定义智能体可以接受的输入数据格式。
  "defaultInputModes": [
    "text/plain",
    "application/json"
  ],
  // 所有技能的默认支持输出 MIME 类型集合，可在每个技能的基础上覆盖。定义智能体可以生成的输出数据格式。
  "defaultOutputModes": [
    "text/plain",
    "application/json"
  ],
  // 智能体可以执行的技能或独特能力集合，每个技能代表一个专门化的功能模块。
  "skills": [
    {
      "id": "beijing-urban-tour.sight-recommender", // 智能体技能的唯一标识符。由提供者定义，建议使用分层命名空间格式。
      "name": "北京城区旅游景点推荐", // 技能的名称，简洁明了地描述技能的主要功能。
      "description": "根据客户需求推荐北京城六区（东城/西城/朝阳/海淀/丰台/石景山）内的旅游景点，提供文化深度体验建议。拒绝郊区景点推荐请求，如八达岭长城、古北水镇等。", // 技能的详细描述，帮助客户端或用户理解其目的、功能范围和限制。应明确说明技能能做什么和不能做什么，包括地理范围、服务类型等限制。
      "version": "1.0.0", // 技能的版本号，由智能体提供者自行定义格式。建议遵循语义化版本控制规范（Semantic Versioning）。格式：MAJOR.MINOR.PATCH，当 API 不兼容时递增 MAJOR 版本。
      // 描述技能能力特征的关键词集合，用于技能发现和匹配。包括功能类型、地域范围、专业领域、目标用户等维度的标签。
      "tags": [
        "旅游",
        "景点推荐",
        "北京",
        "城区",
        "文化体验",
        "行程规划"
      ],
      // 此技能可以处理的示例提示或场景，帮助用户理解如何使用该技能。提供具体的用户输入示例和期望的处理场景。
      "examples": [
        "推荐几个适合带小孩的北京城区景点",
        "不要太累的故宫周边一日游安排",
        "朝阳区有什么文化体验好的地方",
        "预算500元的海淀区半日游"
      ],
      // 此技能支持的输入 MIME 类型集合，覆盖智能体的默认值。定义该技能可以接受的输入数据格式，如文本、图片、音频等。
      "inputModes": [
        "text/plain",
        "application/json"
      ],
      // 此技能支持的输出 MIME 类型集合，覆盖智能体的默认值。定义该技能可以生成的输出数据格式，如文本、结构化数据、图片等。
      "outputModes": [
        "text/plain",
        "application/json",
        "text/markdown"
      ]
    }
  ],
  "entityUserId": "string", // 实体的用户关联 ID。用于将智能体实体与特定用户绑定。用于用户助手类智能体，标识该智能体为某个特定用户提供服务。
  // 实体的额外元数据。具体格式和内容由 Agent Provider 自定义。
  "entityMeta": {
    "location": "Beijing",
    "environment": "production"
  },
  // 证书签发相关配置。用于声明服务端证书需要的 SAN 和期望有效期。
  "certificate": {
    // 需要写入证书 SAN 的 DNS/IP 条目。
    "altNames": {
      // 需要加入证书 DNS SAN 的主机名列表。
      "dns": [
        "mq.acps.example.com",
        "*.acps.example.com"
      ],
      // 需要加入证书 IP SAN 的地址列表。
      "ip": [
        "192.0.2.10",
        "198.51.100.20"
      ]
    },
    "requestedValidity": 49 // 证书期望有效期（天）。最终值由 CA Server 按系统上限裁剪。
  }
}