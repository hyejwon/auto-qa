

data =[{"GameObjectName":"PrimaryDescription","HyperLinkPositions":[{"PositionX":486.80603,"PositionY":748.634033},{"PositionX":239.076859,"PositionY":704.228638}],"Text":"계정 연동을 진행하려면 <link=\"https://www.supermagic.io/base-policy/index.html?category=terms-of-service\"><sprite=0 color=#00a2ff><color=#00a2ff>이용약관</color></link> 및 <link=\"https://www.supermagic.io/base-policy/index.html?category=privacy-policy\"><sprite=0 color=#00a2ff><color=#00a2ff>개인정보처리방침</color></link>을 검토했음을 확인해야 합니다."}]

cand = []
for item in data:  # 네가 준 배열
    cand.extend(parse_links_and_positions(item))

print(cand)
